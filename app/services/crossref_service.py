import asyncio
import re
import unicodedata
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
RATE_LIMIT_SEMAPHORE = asyncio.Semaphore(4)

DEFAULT_HEADERS = {
    "User-Agent": "AcademicCitationVerifier/1.0 (mailto:tuana@nku.edu.tr; Tekirdag Namik Kemal University)"
}


# ---------------------------------------------------------------------------
# 3. HARİCİ AKADEMİK VERİ TABANI İSTEMCİLERİ (Crossref + DataCite + OpenAlex + Semantic Scholar)
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
    """OpenAlex'in ters indeks (inverted index) formatındaki özetini düz metne çevirir."""
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


async def _get_json_with_retry(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """HTTP 429 veya geçici ağ hatalarına karşı Exponential Backoff ile güvenli GET isteği yapar."""
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params, headers=DEFAULT_HEADERS)
            if resp.status_code == 200:
                return 200, resp.json()
            if resp.status_code == 429:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            return resp.status_code, {}
        except httpx.RequestError:
            if attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            return 503, {}
    return 429, {}


async def fetch_by_doi_multi_source(doi: str) -> tuple[int, dict[str, Any]]:
    """
    Katman 1: Verilen DOI'yi sırasıyla Crossref, DataCite ve OpenAlex üzerinde arar.
    Böylece DataCite veya yerel DOI kullanan hakemli yayınlar yanlışlıkla 'Sahte DOI' sayılmaz.
    """
    cache_key = f"doi:{doi.lower()}"
    if cache_key in METADATA_CACHE:
        return 200, METADATA_CACHE[cache_key]

    async with RATE_LIMIT_SEMAPHORE:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            status, data = await _get_json_with_retry(client, f"https://api.crossref.org/works/{doi}")
            if status == 200 and data.get("message"):
                std = _standardize_crossref_item(data["message"])
                METADATA_CACHE[cache_key] = std
                return 200, std

            dc_status, dc_data = await _get_json_with_retry(client, f"https://api.datacite.org/dois/{doi}")
            if dc_status == 200 and dc_data.get("data"):
                std = _standardize_datacite_item(dc_data["data"])
                METADATA_CACHE[cache_key] = std
                return 200, std

            oa_status, oa_data = await _get_json_with_retry(client, f"https://api.openalex.org/works/https://doi.org/{doi}")
            if oa_status == 200 and oa_data.get("id"):
                std = _standardize_openalex_item(oa_data)
                METADATA_CACHE[cache_key] = std
                return 200, std

            return (404 if status == 404 else status), {}


async def search_by_title_and_author(title: str, first_author: str | None = None, year: int | None = None) -> list[dict[str, Any]]:
    """
    Katman 2: DOI'si olmayan hakemli dergi makalelerini, bildirileri ve kitapları
    Başlık + Yazar + Yıl bilgileriyle Crossref, OpenAlex ve Semantic Scholar üzerinden arar.
    """
    clean_title = title.strip()
    if len(clean_title) < 6:
        return []

    cache_key = f"search:{_normalize_for_match(clean_title)}:{_normalize_for_match(first_author or '')}:{year or ''}"
    if cache_key in METADATA_CACHE:
        return METADATA_CACHE[cache_key].get("candidates", [])

    candidates: list[dict[str, Any]] = []

    async with RATE_LIMIT_SEMAPHORE:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            cr_params: dict[str, Any] = {"query.bibliographic": clean_title, "rows": 4}
            if first_author:
                cr_params["query.author"] = first_author
            cr_status, cr_data = await _get_json_with_retry(client, "https://api.crossref.org/works", params=cr_params)
            if cr_status == 200:
                items = (cr_data.get("message") or {}).get("items") or []
                for item in items:
                    candidates.append(_standardize_crossref_item(item))

            oa_params: dict[str, Any] = {"search": clean_title, "per-page": 4}
            oa_status, oa_data = await _get_json_with_retry(client, "https://api.openalex.org/works", params=oa_params)
            if oa_status == 200:
                for item in oa_data.get("results") or []:
                    candidates.append(_standardize_openalex_item(item))

            best_so_far = max((compute_title_similarity(clean_title, c.get("title", "")) for c in candidates), default=0)
            if best_so_far < 75:
                s2_query = f"{clean_title} {first_author or ''}".strip()
                s2_params = {
                    "query": s2_query,
                    "limit": 3,
                    "fields": "title,authors,year,externalIds,venue,abstract,url"
                }
                s2_status, s2_data = await _get_json_with_retry(
                    client,
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    params=s2_params
                )
                if s2_status == 200:
                    for item in s2_data.get("data") or []:
                        candidates.append(_standardize_semanticscholar_item(item))

    METADATA_CACHE[cache_key] = {"candidates": candidates}
    return candidates


# ---------------------------------------------------------------------------
# 4. ÇOK BOYUTLU DOĞRULAMA MOTORU (Başlık + Yazar + Yıl + Sayfa + Metin İçi Bağlam)
# ---------------------------------------------------------------------------

def evaluate_author_match(claimed_authors: list[str], real_authors: list[str], raw_text: str) -> tuple[str, int]:
    """
    Kaynakçadaki yazar soyadlarını API'den dönen gerçek yazar soyadlarıyla karşılaştırır.
    Dönüş: (durum_etiketi, yazar_uyum_skoru_0_100)
    """
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
    """
    Kaynakçadaki yayın yılını API'den dönen gerçek yayın yılıyla karşılaştırır.
    Akademik dergilerde Online First (Erken Görünüm) ile Cilt/Sayı basımı arasındaki ±1 yıl farkını tolere eder.
    """
    if not claimed_year or not real_year:
        return "BELİRTİLMEMİŞ", 80
    diff = abs(claimed_year - real_year)
    if diff == 0:
        return "DOĞRU", 100
    if diff == 1:
        return f"KABUL_EDİLEBİLİR_FARK (±1 Yıl Online/Basım Farkı — Gerçek: {real_year}, Yazılan: {claimed_year})", 85
    return f"HATALI (Gerçek: {real_year}, Yazılan: {claimed_year})", 0


def evaluate_context_relevance(
    context_sentences: list[str],
    real_title: str,
    abstract: str
) -> dict[str, Any]:
    """
    Metin içinde bu kaynağa atıf yapılan cümlelerin (context_sentences)
    makalenin başlığı ve özetiyle (abstract) kavramsal uyumunu denetler.
    """
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
    """
    Seçilen en iyi API adayı ile kaynakçadaki bilgileri (Başlık, Yazar, Yıl, Sayfa, Bağlam)
    karşılaştırarak detaylı doğrulama raporu üretir.
    """
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


async def verify_reference_with_crossref(
    ref_info: dict[str, Any],
    similarity_threshold: int = 80,
    context_sentences: list[str] | None = None
) -> dict[str, Any]:
    """
    Ana Doğrulama Fonksiyonu (main.py ile tam uyumlu):
    1. Kaynakta DOI varsa: Crossref -> DataCite -> OpenAlex sırasıyla DOI'yi doğrular.
    2. Kaynakta DOI yoksa: Crossref + OpenAlex + Semantic Scholar üzerinde Başlık + Yazar + Yıl
       araması yaparak DOI'siz hakemli dergi makalelerini, bildirileri ve kitapları doğrular.
    3. Yazılım dokümantasyonu / resmi web kaynağıysa (URL içeriyorsa) bunu ayrıca sınıflandırır.
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
                    f"KRİTİK HATA: Yazılan DOI ({doi}) Crossref, DataCite ve OpenAlex'te bulunamadı; "
                    f"başlık ve yazardan yapılan aramada da böyle bir hakemli makaleye rastlanmadı (Muhtemel yapay zeka halüsinasyonu / sahte kaynak)!"
                )
            }

        # DURUM B: KAYNAKTA DOI YOK -> BAŞLIK VE YAZARDAN ARAMA (Çoklu API)
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
            if combined > best_combined_score and t_sim >= 65:
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
            "verification_method": "TITLE_AND_AUTHOR_SEARCH_FAILED",
            "confidence_score": 0,
            "extracted_title_searched": extracted_title,
            "searched_author": first_author,
            "message": (
                "Kaynakta DOI yok. Başlık ve yazar bilgisiyle Crossref, OpenAlex ve Semantic Scholar "
                "üzerinde yapılan aramada eşleşen bir yayın bulunamadı. Manuel kontrol önerilir."
            )
        }

    except Exception as e:
        return {
            "status": "TIMEOUT_OR_ERROR",
            "verification_method": "EXCEPTION",
            "message": f"Doğrulama sırasında beklenmeyen hata: {str(e)}"
        }