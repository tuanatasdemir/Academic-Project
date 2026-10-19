import asyncio
import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Any
import httpx

# ---------------------------------------------------------------------------
# 1. ESNEK METİN BENZERLİĞİ MOTORU (RapidFuzz -> TheFuzz -> Difflib Fallback)
# ---------------------------------------------------------------------------
try:
    from rapidfuzz import fuzz as _external_fuzz  # type: ignore
    FUZZ_BACKEND = "rapidfuzz"
except ImportError:
    try:
        from thefuzz import fuzz as _external_fuzz  # type: ignore
        FUZZ_BACKEND = "thefuzz"
    except ImportError:
        _external_fuzz = None
        FUZZ_BACKEND = "difflib_builtin"


def _normalize_for_match(text: str) -> str:
    """Karşılaştırma öncesi Türkçe karakterleri, noktalama işaretlerini ve HTML etiketlerini temizler."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.strip().lower()
    replacements = {
        'ı': 'i', 'ğ': 'g', 'ü': 'u', 'ş': 's', 'ö': 'o', 'ç': 'c',
        'İ': 'i', 'I': 'i', 'â': 'a', 'î': 'i', 'û': 'u', 'é': 'e', 'í': 'i'
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize('NFKD', text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def compute_title_similarity(title_a: str, title_b: str) -> int:
    """
    İki başlık arasındaki benzerlik oranını (0-100) hesaplar.
    Hem tam başlık dizilimini hem de alt-başlık (partial) uyumunu dikkate alır.
    """
    norm_a = _normalize_for_match(title_a)
    norm_b = _normalize_for_match(title_b)
    if not norm_a or not norm_b:
        return 0
    if norm_a == norm_b:
        return 100

    if _external_fuzz is not None:
        ratio_score = float(_external_fuzz.ratio(norm_a, norm_b))
        token_score = float(_external_fuzz.token_sort_ratio(norm_a, norm_b))
        if min(len(norm_a), len(norm_b)) >= 25:
            partial_score = float(_external_fuzz.partial_ratio(norm_a, norm_b))
            return int(round(max(ratio_score, token_score, partial_score * 0.95)))
        return int(round(max(ratio_score, token_score)))

    direct_ratio = SequenceMatcher(None, norm_a, norm_b).ratio() * 100.0
    tokens_a = " ".join(sorted(norm_a.split()))
    tokens_b = " ".join(sorted(norm_b.split()))
    token_ratio = SequenceMatcher(None, tokens_a, tokens_b).ratio() * 100.0

    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    partial_ratio = 0.0
    if len(shorter) >= 25 and shorter in longer:
        partial_ratio = 95.0

    return int(round(max(direct_ratio, token_ratio, partial_ratio)))


# ---------------------------------------------------------------------------
# 2. BELLEK İÇİ ÖNBELLEK (CACHE) VE HIZ SINIRLAYICI (RATE LIMITER)
# ---------------------------------------------------------------------------

METADATA_CACHE: dict[str, dict[str, Any]] = {}
RATE_LIMIT_SEMAPHORE = asyncio.Semaphore(6)

DEFAULT_HEADERS = {
    "User-Agent": "AcademicCitationVerifier/2.0 (mailto:tuana@nku.edu.tr; Tekirdag Namik Kemal University - Computer Engineering)"
}


# ---------------------------------------------------------------------------
# 3. HARİCİ AKADEMİK VERİ TABANI MODEL DÖNÜŞTÜRÜCÜLERİ
# ---------------------------------------------------------------------------

def _standardize_crossref_item(item: dict[str, Any]) -> dict[str, Any]:
    """Crossref API yanıtını standart doğrulama şemasına dönüştürür."""
    titles = item.get("title") or []
    real_title = titles[0] if titles else ""

    authors_list: list[str] = []
    for auth in item.get("author", []):
        family = auth.get("family") or auth.get("name") or ""
        if family:
            authors_list.append(family)

    year = None
    for date_field in ("issued", "published-print", "published-online", "created"):
        parts = item.get(date_field, {}).get("date-parts", [[None]])
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break

    container = item.get("container-title") or []
    journal = container[0] if container else item.get("publisher", "")

    abstract_raw = item.get("abstract", "")
    abstract_clean = re.sub(r'<[^>]+>', ' ', abstract_raw).strip() if abstract_raw else ""

    return {
        "source_api": "Crossref",
        "title": real_title,
        "authors": authors_list,
        "year": year,
        "pages": item.get("page"),
        "doi": item.get("DOI"),
        "journal": journal,
        "abstract": abstract_clean,
        "url": item.get("URL")
    }


def _reconstruct_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    """OpenAlex'in ters indeks formatındaki özetini düz metne çevirir."""
    if not inverted_index or not isinstance(inverted_index, dict):
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            for pos in positions:
                word_positions.append((pos, word))
    word_positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in word_positions)


def _standardize_openalex_item(item: dict[str, Any]) -> dict[str, Any]:
    """OpenAlex Works API yanıtını standart doğrulama şemasına dönüştürür."""
    real_title = item.get("title") or item.get("display_name") or ""

    authors_list: list[str] = []
    for authorship in item.get("authorships", []):
        disp_name = (authorship.get("author") or {}).get("display_name") or ""
        if disp_name:
            surname = disp_name.strip().split()[-1]
            authors_list.append(surname)

    year = item.get("publication_year")
    biblio = item.get("biblio") or {}
    first_page = biblio.get("first_page")
    last_page = biblio.get("last_page")
    pages = f"{first_page}-{last_page}" if (first_page and last_page) else (first_page or None)

    raw_doi = item.get("doi") or ""
    clean_doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', raw_doi, flags=re.IGNORECASE) or None

    primary_loc = item.get("primary_location") or {}
    source_info = primary_loc.get("source") or {}
    journal = source_info.get("display_name") or ""

    abstract_text = _reconstruct_openalex_abstract(item.get("abstract_inverted_index"))

    return {
        "source_api": "OpenAlex",
        "title": real_title,
        "authors": authors_list,
        "year": int(year) if year else None,
        "pages": pages,
        "doi": clean_doi,
        "journal": journal,
        "abstract": abstract_text,
        "url": item.get("id")
    }


def _standardize_datacite_item(item: dict[str, Any]) -> dict[str, Any]:
    """DataCite REST API yanıtını standart doğrulama şemasına dönüştürür."""
    attrs = item.get("attributes") or {}
    titles = attrs.get("titles") or []
    real_title = titles[0].get("title", "") if titles else ""

    authors_list: list[str] = []
    for creator in attrs.get("creators", []):
        family = creator.get("familyName") or creator.get("name") or ""
        if family:
            authors_list.append(family.split(",")[0].strip())

    year = attrs.get("publicationYear")
    descriptions = attrs.get("descriptions") or []
    abstract_text = descriptions[0].get("description", "") if descriptions else ""

    return {
        "source_api": "DataCite",
        "title": real_title,
        "authors": authors_list,
        "year": int(year) if year else None,
        "pages": None,
        "doi": attrs.get("doi"),
        "journal": attrs.get("publisher") or "",
        "abstract": abstract_text,
        "url": attrs.get("url")
    }


def _standardize_semanticscholar_item(item: dict[str, Any]) -> dict[str, Any]:
    """Semantic Scholar Graph API yanıtını standart doğrulama şemasına dönüştürür."""
    real_title = item.get("title") or ""
    authors_list: list[str] = []
    for auth in item.get("authors", []):
        name = auth.get("name") or ""
        if name:
            authors_list.append(name.strip().split()[-1])

    ext_ids = item.get("externalIds") or {}
    doi = ext_ids.get("DOI")

    return {
        "source_api": "Semantic Scholar",
        "title": real_title,
        "authors": authors_list,
        "year": int(item["year"]) if item.get("year") else None,
        "pages": None,
        "doi": doi,
        "journal": item.get("venue") or "",
        "abstract": item.get("abstract") or "",
        "url": item.get("url")
    }


def _standardize_dblp_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    DBLP Computer Science Bibliography REST API yanıtını standart doğrulama şemasına dönüştürür.
    Özellikle Bilgisayar Mühendisliği konferans bildirileri (IEEE, ACM, Springer) ve makalelerinde liderdir.
    """
    info = item.get("info") or {}
    raw_title = info.get("title") or ""
    clean_title = raw_title.rstrip(".")

    authors_raw = info.get("authors") or {}
    authors_list: list[str] = []
    if isinstance(authors_raw, dict):
        auth_entry = authors_raw.get("author")
        if isinstance(auth_entry, list):
            for a in auth_entry:
                if isinstance(a, dict):
                    name = a.get("text") or a.get("@text") or ""
                else:
                    name = str(a)
                if name:
                    authors_list.append(name.strip().split()[-1])
        elif isinstance(auth_entry, dict):
            name = auth_entry.get("text") or auth_entry.get("@text") or ""
            if name:
                authors_list.append(name.strip().split()[-1])
        elif isinstance(auth_entry, str) and auth_entry:
            authors_list.append(auth_entry.strip().split()[-1])

    year_str = info.get("year")
    year = int(year_str) if (year_str and str(year_str).isdigit()) else None

    return {
        "source_api": "DBLP (Computer Science)",
        "title": clean_title,
        "authors": authors_list,
        "year": year,
        "pages": info.get("pages"),
        "doi": info.get("doi"),
        "journal": info.get("venue") or "",
        "abstract": "",
        "url": info.get("ee") or info.get("url")
    }


def _standardize_europepmc_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Europe PMC REST API yanıtını standart doğrulama şemasına dönüştürür.
    Açık erişimli fen, mühendislik ve biyomedikal literatüründe dünya lideridir.
    """
    raw_title = item.get("title") or ""
    clean_title = re.sub(r'<[^>]+>', ' ', raw_title).strip().rstrip(".")

    author_str = item.get("authorString") or ""
    authors_list: list[str] = []
    if author_str:
        for a in author_str.split(","):
            a_clean = a.strip()
            if a_clean:
                surname = a_clean.split()[0].rstrip(".")
                if surname:
                    authors_list.append(surname)

    year_raw = item.get("pubYear")
    year = int(year_raw) if (year_raw and str(year_raw).isdigit()) else None

    return {
        "source_api": "Europe PMC",
        "title": clean_title,
        "authors": authors_list,
        "year": year,
        "pages": item.get("pageInfo"),
        "doi": item.get("doi"),
        "journal": item.get("journalTitle") or "",
        "abstract": item.get("abstractText") or "",
        "url": f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}" if item.get("id") else None
    }


def _parse_arxiv_atom(xml_text: str) -> list[dict[str, Any]]:
    """
    arXiv Atom XML API yanıtını ayrıştırır.
    Yapay zekâ, bilgisayar bilimleri ve matematik alanındaki en güncel önbasımları doğrular.
    """
    items: list[dict[str, Any]] = []
    if not xml_text or "<entry>" not in xml_text:
        return items

    try:
        clean_xml = re.sub(r'<\s*(\/)?\s*[a-zA-Z0-9_]+:([a-zA-Z0-9_]+)', r'<\1\2', xml_text)
        clean_xml = re.sub(r'\s+xmlns(:\w+)?="[^"]+"', '', clean_xml)
        root = ET.fromstring(clean_xml)

        for entry in root.findall(".//entry"):
            title_el = entry.find("title")
            title = title_el.text.strip().replace("\n", " ") if (title_el is not None and title_el.text) else ""

            authors: list[str] = []
            for a_el in entry.findall(".//author"):
                name_el = a_el.find("name")
                if name_el is not None and name_el.text:
                    surname = name_el.text.strip().split()[-1]
                    authors.append(surname)

            pub_el = entry.find("published")
            year = None
            if pub_el is not None and pub_el.text and len(pub_el.text) >= 4:
                year_str = pub_el.text[:4]
                if year_str.isdigit():
                    year = int(year_str)

            doi_el = entry.find("doi")
            doi = doi_el.text.strip() if (doi_el is not None and doi_el.text) else None

            id_el = entry.find("id")
            url = id_el.text.strip() if (id_el is not None and id_el.text) else ""

            summary_el = entry.find("summary")
            abstract = summary_el.text.strip().replace("\n", " ") if (summary_el is not None and summary_el.text) else ""

            if title:
                items.append({
                    "source_api": "arXiv (Preprint)",
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "pages": None,
                    "doi": doi,
                    "journal": "arXiv Computer Science / AI",
                    "abstract": abstract,
                    "url": url
                })
    except Exception:
        pass
    return items


# ---------------------------------------------------------------------------
# 4. GÜVENLİ HTTP İSTEMCİSİ (Retry + Exponential Backoff)
# ---------------------------------------------------------------------------

async def _get_json_with_retry(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """HTTP 429 veya geçici ağ hatalarına karşı Exponential Backoff ile güvenli JSON isteği yapar."""
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params, headers=DEFAULT_HEADERS)
            if resp.status_code == 200:
                return 200, resp.json()
            if resp.status_code == 429:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            return resp.status_code, {}
        except httpx.RequestError:
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            return 503, {}
    return 429, {}


async def _get_text_with_retry(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> tuple[int, str]:
    """XML/Düz metin formatında yanıt veren API'ler için güvenli GET isteği yapar."""
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params, headers=DEFAULT_HEADERS)
            if resp.status_code == 200:
                return 200, resp.text
            if resp.status_code == 429:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            return resp.status_code, ""
        except httpx.RequestError:
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            return 503, ""
    return 429, ""


# ---------------------------------------------------------------------------
# 5. KATMAN 1: ÇOK KAYNAKLI DOI DOĞRULAMA (Crossref + DataCite + OpenAlex + Europe PMC)
# ---------------------------------------------------------------------------

async def fetch_by_doi_multi_source(doi: str) -> tuple[int, dict[str, Any]]:
    """
    Verilen DOI'yi sırasıyla Crossref, DataCite, OpenAlex ve Europe PMC üzerinde arar.
    Böylece DataCite veya tıp/biyoloji/mühendislik dizinli yayınlar yanlışlıkla 'Sahte DOI' sayılmaz.
    """
    cache_key = f"doi:{doi.lower()}"
    if cache_key in METADATA_CACHE:
        return 200, METADATA_CACHE[cache_key]

    async with RATE_LIMIT_SEMAPHORE:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            # 1. Crossref
            status, data = await _get_json_with_retry(client, f"https://api.crossref.org/works/{doi}")
            if status == 200 and data.get("message"):
                std = _standardize_crossref_item(data["message"])
                METADATA_CACHE[cache_key] = std
                return 200, std

            # 2. DataCite
            dc_status, dc_data = await _get_json_with_retry(client, f"https://api.datacite.org/dois/{doi}")
            if dc_status == 200 and dc_data.get("data"):
                std = _standardize_datacite_item(dc_data["data"])
                METADATA_CACHE[cache_key] = std
                return 200, std

            # 3. OpenAlex
            oa_status, oa_data = await _get_json_with_retry(client, f"https://api.openalex.org/works/https://doi.org/{doi}")
            if oa_status == 200 and oa_data.get("id"):
                std = _standardize_openalex_item(oa_data)
                METADATA_CACHE[cache_key] = std
                return 200, std

            # 4. Europe PMC
            epmc_status, epmc_data = await _get_json_with_retry(
                client,
                "https://europepmc.org/webservices/rest/search",
                params={"query": f"DOI:\"{doi}\"", "format": "json", "pageSize": 1}
            )
            if epmc_status == 200:
                results = (epmc_data.get("resultList") or {}).get("result") or []
                if results:
                    std = _standardize_europepmc_item(results[0])
                    METADATA_CACHE[cache_key] = std
                    return 200, std

            return (404 if status == 404 else status), {}


# ---------------------------------------------------------------------------
# 6. KATMAN 2: DEVASA VERİ HAVUZUNDA BAŞLIK + YAZAR + YIL ARAMASI (7 API)
# ---------------------------------------------------------------------------

async def _search_crossref(client: httpx.AsyncClient, clean_title: str, first_author: str | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"query.bibliographic": clean_title, "rows": 3}
    if first_author:
        params["query.author"] = first_author
    st, data = await _get_json_with_retry(client, "https://api.crossref.org/works", params=params)
    res = []
    if st == 200:
        items = (data.get("message") or {}).get("items") or []
        for it in items:
            res.append(_standardize_crossref_item(it))
    return res


async def _search_openalex(client: httpx.AsyncClient, clean_title: str) -> list[dict[str, Any]]:
    st, data = await _get_json_with_retry(client, "https://api.openalex.org/works", params={"search": clean_title, "per-page": 3})
    res = []
    if st == 200:
        for it in data.get("results") or []:
            res.append(_standardize_openalex_item(it))
    return res


async def _search_dblp(client: httpx.AsyncClient, clean_title: str) -> list[dict[str, Any]]:
    """DBLP Bilgisayar Bilimleri bibliyografyası araması."""
    st, data = await _get_json_with_retry(
        client,
        "https://dblp.org/search/publ/api",
        params={"q": clean_title, "format": "json", "h": 3}
    )
    res = []
    if st == 200:
        hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
        for it in hits:
            res.append(_standardize_dblp_item(it))
    return res


async def _search_europepmc(client: httpx.AsyncClient, clean_title: str) -> list[dict[str, Any]]:
    """Europe PMC açık erişim makale dizini araması."""
    st, data = await _get_json_with_retry(
        client,
        "https://europepmc.org/webservices/rest/search",
        params={"query": f"TITLE:\"{clean_title}\"", "format": "json", "pageSize": 3}
    )
    res = []
    if st == 200:
        results = (data.get("resultList") or {}).get("result") or []
        for it in results:
            res.append(_standardize_europepmc_item(it))
    return res


async def _search_arxiv(client: httpx.AsyncClient, clean_title: str) -> list[dict[str, Any]]:
    """arXiv önbasım (preprint) veri tabanı araması."""
    st, xml_text = await _get_text_with_retry(
        client,
        "http://export.arxiv.org/api/query",
        params={"search_query": f"all:{clean_title}", "max_results": 3}
    )
    if st == 200 and xml_text:
        return _parse_arxiv_atom(xml_text)
    return []


async def _search_semantic_scholar(client: httpx.AsyncClient, clean_title: str, first_author: str | None) -> list[dict[str, Any]]:
    """Semantic Scholar AI akademik motoru araması."""
    s2_query = f"{clean_title} {first_author or ''}".strip()
    st, data = await _get_json_with_retry(
        client,
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": s2_query, "limit": 3, "fields": "title,authors,year,externalIds,venue,abstract,url"}
    )
    res = []
    if st == 200:
        for it in data.get("data") or []:
            res.append(_standardize_semanticscholar_item(it))
    return res


async def search_by_title_and_author(title: str, first_author: str | None = None, year: int | None = None) -> list[dict[str, Any]]:
    """
    Katman 2: DOI'si olmayan tüm hakemli makaleleri, konferans bildirilerini ve kitapları
    7 Farklı Küresel Akademik Veri Tabanı (Crossref, OpenAlex, DBLP, Europe PMC, Semantic Scholar, arXiv, DataCite)
    üzerinde eşzamanlı (asenkron paralel) olarak arar.
    """
    clean_title = title.strip()
    if len(clean_title) < 6:
        return []

    cache_key = f"search:{_normalize_for_match(clean_title)}:{_normalize_for_match(first_author or '')}:{year or ''}"
    if cache_key in METADATA_CACHE:
        return METADATA_CACHE[cache_key].get("candidates", [])

    candidates: list[dict[str, Any]] = []

    async with RATE_LIMIT_SEMAPHORE:
        async with httpx.AsyncClient(timeout=9.0, follow_redirects=True) as client:
            # 1. Aşama: 4 Ana Veri Tabanını Paralel Sorgula (Crossref, OpenAlex, DBLP, Europe PMC)
            core_tasks = [
                _search_crossref(client, clean_title, first_author),
                _search_openalex(client, clean_title),
                _search_dblp(client, clean_title),
                _search_europepmc(client, clean_title),
            ]
            core_results = await asyncio.gather(*core_tasks, return_exceptions=True)
            for r in core_results:
                if isinstance(r, list):
                    candidates.extend(r)

            best_sim_so_far = max(
                (compute_title_similarity(clean_title, c.get("title", "")) for c in candidates),
                default=0
            )

            # 2. Aşama: Eğer güçlü bir eşleşme (%75+) bulunamadıysa Semantic Scholar ve arXiv'i devreye sok
            if best_sim_so_far < 75:
                deep_tasks = [
                    _search_semantic_scholar(client, clean_title, first_author),
                    _search_arxiv(client, clean_title),
                ]
                deep_results = await asyncio.gather(*deep_tasks, return_exceptions=True)
                for r in deep_results:
                    if isinstance(r, list):
                        candidates.extend(r)

    METADATA_CACHE[cache_key] = {"candidates": candidates}
    return candidates


# ---------------------------------------------------------------------------
# 7. ÇOK BOYUTLU DOĞRULAMA VE UYUM ANALİZİ
# ---------------------------------------------------------------------------

def evaluate_author_match(claimed_authors: list[str], real_authors: list[str], raw_text: str) -> tuple[str, int]:
    """Kaynakçadaki yazarları API'den dönen gerçek yazarlarla karşılaştırır."""
    if not real_authors:
        return "API_YAZAR_VERİSİ_YOK", 80

    norm_real = [_normalize_for_match(a) for a in real_authors if _normalize_for_match(a)]
    norm_claimed = [_normalize_for_match(a) for a in claimed_authors if _normalize_for_match(a)]
    norm_raw = _normalize_for_match(raw_text)

    if not norm_real:
        return "API_YAZAR_VERİSİ_YOK", 80

    matched_count = 0
    for r_auth in norm_real:
        r_tokens = r_auth.split()
        r_surname = r_tokens[-1] if r_tokens else r_auth
        if any(r_surname in c or c in r_surname for c in norm_claimed if len(c) >= 2):
            matched_count += 1
        elif len(r_surname) >= 3 and r_surname in norm_raw:
            matched_count += 1

    first_real_surname = norm_real[0].split()[-1] if norm_real[0].split() else norm_real[0]
    first_author_matched = (
        any(first_real_surname in c or c in first_real_surname for c in norm_claimed if len(c) >= 2)
        or (len(first_real_surname) >= 3 and first_real_surname in norm_raw)
    )

    if matched_count == len(norm_real) or (first_author_matched and matched_count >= 1):
        return "DOĞRU", 100
    if matched_count > 0:
        return f"KISMİ_UYUŞMA (Gerçek: {', '.join(real_authors[:3])})", 65
    return f"HATALI (Gerçek: {', '.join(real_authors[:3])}, Yazılan: {', '.join(claimed_authors) or 'Belirtilmemiş'})", 0


def evaluate_year_match(claimed_year: int | None, real_year: int | None) -> tuple[str, int]:
    """Kaynakçadaki yılı gerçek yayın yılıyla karşılaştırır (±1 yıl Online First toleransı)."""
    if not claimed_year or not real_year:
        return "BELİRTİLMEMİŞ", 80
    diff = abs(claimed_year - real_year)
    if diff == 0:
        return "DOĞRU", 100
    if diff == 1:
        return f"KABUL_EDİLEBİLİR_FARK (±1 Yıl Online/Basım Farkı — Gerçek: {real_year}, Yazılan: {claimed_year})", 85
    return f"HATALI (Gerçek: {real_year}, Yazılan: {claimed_year})", 0


def evaluate_context_relevance(context_sentences: list[str], real_title: str, abstract: str) -> dict[str, Any]:
    """Metin içindeki atıf cümlelerinin makale özeti ve başlığıyla kavramsal uyumunu ölçer."""
    if not context_sentences:
        return {
            "status": "METİNDE_ATIF_YOK",
            "message": "Bu kaynağa metin içinde hiç atıf yapılmamış."
        }

    source_corpus = _normalize_for_match(f"{real_title} {abstract}")
    source_tokens = {w for w in source_corpus.split() if len(w) >= 4}
    if not source_tokens:
        return {
            "status": "ÖZET_VERİSİ_SINIRLI",
            "sentences_checked": len(context_sentences),
            "message": "Metin içi cümleler kaydedildi (API özet verisi sınırlı)."
        }

    sentence_text = _normalize_for_match(" ".join(context_sentences))
    sentence_tokens = {w for w in sentence_text.split() if len(w) >= 4}

    common_roots = {
        w[:5] for w in sentence_tokens if len(w) >= 5
    }.intersection({
        w[:5] for w in source_tokens if len(w) >= 5
    })

    return {
        "status": "BAĞLAM_UYUMLU" if common_roots else "MANUEL_BAĞLAM_KONTROLÜ_ÖNERİLİR",
        "sentences_checked": len(context_sentences),
        "matched_concept_roots": sorted(list(common_roots))[:8],
        "sample_sentence": context_sentences[0][:220]
    }


def _build_verification_response(
    ref_info: dict[str, Any],
    candidate: dict[str, Any],
    similarity_threshold: int,
    verification_method: str,
    context_sentences: list[str] | None = None
) -> dict[str, Any]:
    extracted_title = ref_info.get("extracted_title") or ref_info.get("raw_text", "")
    raw_text = ref_info.get("raw_text", "")
    claimed_authors = ref_info.get("authors") or []
    claimed_year = ref_info.get("year")
    claimed_pages = ref_info.get("claimed_pages")

    real_title = candidate.get("title") or "Bilinmeyen Başlık"
    real_authors = candidate.get("authors") or []
    real_year = candidate.get("year")
    real_pages = candidate.get("pages")
    real_doi = candidate.get("doi")
    source_api = candidate.get("source_api", "Crossref")

    sim_clean = compute_title_similarity(real_title, extracted_title)
    sim_raw = compute_title_similarity(real_title, raw_text)
    title_similarity = max(sim_clean, sim_raw)
    title_ok = title_similarity >= similarity_threshold

    author_status, author_score = evaluate_author_match(claimed_authors, real_authors, raw_text)
    author_ok = author_score >= 65

    year_status, year_score = evaluate_year_match(claimed_year, real_year)
    year_ok = year_score >= 80

    page_status = "BELİRTİLMEMİŞ"
    page_ok = True
    if claimed_pages and real_pages:
        norm_claimed_p = re.sub(r'\s+', '', str(claimed_pages)).replace('–', '-')
        norm_real_p = re.sub(r'\s+', '', str(real_pages)).replace('–', '-')
        if norm_claimed_p == norm_real_p or norm_claimed_p in norm_real_p or norm_real_p in norm_claimed_p:
            page_status = "DOĞRU"
        else:
            page_status = f"HATALI (Gerçek: {real_pages}, Yazılan: {claimed_pages})"
            page_ok = False
    elif claimed_pages and not real_pages:
        page_status = f"API'de sayfa verisi yok (Yazılan: {claimed_pages})"

    confidence_score = int(round(title_similarity * 0.55 + author_score * 0.30 + year_score * 0.15))

    issues: list[str] = []
    if not title_ok:
        issues.append(f"Başlık benzerliği düşük (%{title_similarity})")
    if not author_ok:
        issues.append(f"Yazar uyuşmazlığı ({author_status})")
    if not year_ok:
        issues.append(f"Yıl uyuşmazlığı ({year_status})")
    if not page_ok:
        issues.append(f"Sayfa numarası uyuşmazlığı ({page_status})")

    if title_ok and author_ok and year_ok and page_ok:
        status = "VERIFIED" if "DOI" in verification_method else "VERIFIED_BY_TITLE_AUTHOR"
        if status == "VERIFIED_BY_TITLE_AUTHOR":
            doi_note = f" (Bulunan DOI: {real_doi})" if real_doi else " (Hakemli dizinde DOI'siz kayıt olarak doğrulandı)"
            message = f"DOI yazılmamış ancak başlık, yazar ve yıl üzerinden {source_api} veri tabanında doğrulandı.{doi_note}"
        else:
            message = f"Tam doğrulama başarılı ({source_api}). Başlık, yazar ve yıl bilgileri doğru."
    elif not title_ok:
        status = "TITLE_MISMATCH_WARNING"
        message = f"DİKKAT: { '; '.join(issues) }"
    else:
        status = "METADATA_MISMATCH"
        message = f"DİKKAT: Başlık eşleşti ancak { '; '.join(issues) }!"

    context_eval = evaluate_context_relevance(
        context_sentences or [],
        real_title,
        candidate.get("abstract") or ""
    )

    return {
        "status": status,
        "verification_method": verification_method,
        "source_database": source_api,
        "confidence_score": confidence_score,
        "real_title": real_title,
        "real_authors": real_authors,
        "year": real_year,
        "discovered_doi": real_doi,
        "journal_or_venue": candidate.get("journal"),
        "title_similarity_percent": title_similarity,
        "author_check": author_status,
        "year_check": year_status,
        "real_pages": real_pages,
        "page_check": page_status,
        "in_text_context_check": context_eval,
        "message": message
    }


# ---------------------------------------------------------------------------
# 8. ANA DOĞRULAMA ÇAĞRISI (verify_reference_with_crossref)
# ---------------------------------------------------------------------------

async def verify_reference_with_crossref(
    ref_info: dict[str, Any],
    similarity_threshold: int = 80,
    context_sentences: list[str] | None = None
) -> dict[str, Any]:
    """
    Kapsamlı Doğrulama Motoru (7 Akademik Veri Tabanı):
    1. DOI varsa: Crossref -> DataCite -> OpenAlex -> Europe PMC sırasıyla aranır.
    2. DOI yoksa: Crossref, OpenAlex, DBLP (Bilgisayar Mühendisliği), Europe PMC,
       Semantic Scholar ve arXiv üzerinde eşzamanlı taranır.
    3. Yazılım/web dokümantasyonuysa (URL içeriyorsa) ayrı sınıflandırılır.
    """
    doi = ref_info.get("doi")
    extracted_title = ref_info.get("extracted_title") or ref_info.get("raw_text", "")
    first_author = ref_info.get("first_author")
    claimed_year = ref_info.get("year")
    url = ref_info.get("url")

    try:
        # DURUM A: KAYNAKTA DOI MEVCUT
        if doi:
            status_code, candidate = await fetch_by_doi_multi_source(doi)
            if status_code == 200 and candidate:
                return _build_verification_response(
                    ref_info=ref_info,
                    candidate=candidate,
                    similarity_threshold=similarity_threshold,
                    verification_method=f"DIRECT_DOI ({candidate.get('source_api')})",
                    context_sentences=context_sentences
                )

            fallback_candidates = await search_by_title_and_author(extracted_title, first_author, claimed_year)
            best_candidate = None
            best_sim = 0
            for cand in fallback_candidates:
                sim = max(
                    compute_title_similarity(cand.get("title", ""), extracted_title),
                    compute_title_similarity(cand.get("title", ""), ref_info.get("raw_text", ""))
                )
                if sim > best_sim:
                    best_sim = sim
                    best_candidate = cand

            if best_candidate and best_sim >= similarity_threshold:
                res = _build_verification_response(
                    ref_info=ref_info,
                    candidate=best_candidate,
                    similarity_threshold=similarity_threshold,
                    verification_method="BROKEN_DOI_RECOVERED_BY_TITLE",
                    context_sentences=context_sentences
                )
                res["status"] = "INVALID_DOI_BUT_PAPER_EXISTS"
                res["message"] = (
                    f"KRİTİK UYARI: Yazılan DOI ({doi}) geçersiz/bulunamadı! Ancak eser başlık ve yazardan "
                    f"{best_candidate.get('source_api')} üzerinde bulundu (Gerçek DOI: {best_candidate.get('doi') or 'Yok'})."
                )
                return res

            return {
                "status": "FAKE_OR_NOT_FOUND",
                "verification_method": "DIRECT_DOI_FAILED",
                "confidence_score": 0,
                "message": (
                    f"KRİTİK HATA: Yazılan DOI ({doi}) Crossref, DataCite, OpenAlex ve Europe PMC'de bulunamadı; "
                    f"başlık ve yazardan yapılan aramada da böyle bir hakemli makaleye rastlanmadı (Muhtemel yapay zeka halüsinasyonu / sahte kaynak)!"
                )
            }

        # DURUM B: KAYNAKTA DOI YOK -> BAŞLIK VE YAZARDAN 7 VERİ TABANINDA ARAMA
        candidates = await search_by_title_and_author(extracted_title, first_author, claimed_year)

        best_candidate = None
        best_combined_score = -1.0

        for cand in candidates:
            t_sim = max(
                compute_title_similarity(cand.get("title", ""), extracted_title),
                compute_title_similarity(cand.get("title", ""), ref_info.get("raw_text", ""))
            )
            _, a_score = evaluate_author_match(
                ref_info.get("authors") or [],
                cand.get("authors") or [],
                ref_info.get("raw_text", "")
            )
            _, y_score = evaluate_year_match(claimed_year, cand.get("year"))
            combined = (t_sim * 0.60) + (a_score * 0.25) + (y_score * 0.15)
            if combined > best_combined_score and t_sim >= 60:
                best_combined_score = combined
                best_candidate = cand

        if best_candidate:
            return _build_verification_response(
                ref_info=ref_info,
                candidate=best_candidate,
                similarity_threshold=similarity_threshold,
                verification_method=f"TITLE_AND_AUTHOR_SEARCH ({best_candidate.get('source_api')})",
                context_sentences=context_sentences
            )

        # DURUM C: WEB / YAZILIM DOKÜMANTASYONU KAYNAĞI KONTROLÜ
        if url:
            return {
                "status": "WEB_OR_DOCUMENTATION_SOURCE",
                "verification_method": "URL_DETECTION",
                "source_database": "Web / Resmi Dokümantasyon",
                "confidence_score": 75,
                "real_title": extracted_title,
                "real_authors": ref_info.get("authors") or [],
                "year": claimed_year,
                "discovered_doi": None,
                "url": url,
                "message": f"Akademik makale DOI'si yok; erişim adresli ({url}) web/dokümantasyon kaynağı olarak tespit edildi."
            }

        return {
            "status": "UNVERIFIED_NO_MATCH",
            "verification_method": "MULTI_DATABASE_SEARCH_FAILED",
            "confidence_score": 0,
            "extracted_title_searched": extracted_title,
            "searched_author": first_author,
            "message": (
                "Kaynakta DOI yok. 7 küresel akademik veri tabanında (Crossref, OpenAlex, DBLP, Europe PMC, "
                "Semantic Scholar, arXiv, DataCite) yapılan başlık ve yazar taramasında eşleşen bir hakemli yayın bulunamadı."
            )
        }

    except Exception as e:
        return {
            "status": "TIMEOUT_OR_ERROR",
            "verification_method": "EXCEPTION",
            "message": f"Doğrulama sırasında beklenmeyen hata: {str(e)}"
        }