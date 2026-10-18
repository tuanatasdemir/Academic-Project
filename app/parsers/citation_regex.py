import re
import unicodedata
from typing import Any
from docx.text.paragraph import Paragraph
from docx.table import Table


# ---------------------------------------------------------------------------
# 1. BELGE OKUMA YARDIMCISI (Paragraflar + Tablolar)
# ---------------------------------------------------------------------------

def extract_all_paragraphs(doc: Any) -> list[str]:
    """
    python-docx Document nesnesinden hem ana metin paragraflarını hem de
    tabloların içindeki hücre metinlerini belge sırasını koruyarak çıkarır.
    Kapak sayfası veya içindekiler tablosu dışındaki literatür tablolarında
    geçen atıfların ([1], (Yazar, 2023)) kaçırılmasını önler.
    """
    paragraphs: list[str] = []

    for child in doc.element.body.iterchildren():
        if child.tag.endswith('p'):
            text = Paragraph(child, doc).text.strip()
            if text:
                paragraphs.append(text)
        elif child.tag.endswith('tbl'):
            tbl = Table(child, doc)
            for row in tbl.rows:
                row_cells_text = []
                for cell in row.cells:
                    cell_text = " ".join(
                        p.text.strip() for p in cell.paragraphs if p.text.strip()
                    )
                    if cell_text and (not row_cells_text or cell_text != row_cells_text[-1]):
                        row_cells_text.append(cell_text)
                if row_cells_text:
                    paragraphs.append(" | ".join(row_cells_text))

    return paragraphs


# ---------------------------------------------------------------------------
# 2. YARDIMCI NORMALİZASYON VE BAŞLIK KONTROL FONKSİYONLARI
# ---------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    """Türkçe karakterleri ve büyük/küçük harf farklarını karşılaştırma için normalize eder."""
    if not text:
        return ""
    text = text.strip().lower()
    replacements = {
        'ı': 'i', 'ğ': 'g', 'ü': 'u', 'ş': 's', 'ö': 'o', 'ç': 'c',
        'İ': 'i', 'I': 'i', 'â': 'a', 'î': 'i', 'û': 'u'
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize('NFKD', text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9\s]', '', text).strip()


def is_bibliography_header(line: str) -> bool:
    """
    Bir paragrafın gerçekten 'Kaynakça / References' bölüm başlığı olup olmadığını denetler.
    - 'Kaynakça yönetimi...' gibi normal cümleleri reddeder.
    - '7. Kaynakça \t 7' gibi İçindekiler (Table of Contents) satırlarını reddeder.
    - 'Kaynakça', '7. KAYNAKÇA', 'KAYNAKLAR', 'References', 'Bibliography' başlıklarını kabul eder.
    """
    cleaned = line.strip()
    if not cleaned or len(cleaned) > 45:
        return False

    # İçindekiler satırı kontrolü: sonunda tab + sayfa numarası veya ".... 7" varsa başlık değildir
    if re.search(r'(\.{2,}|\t+)\s*\d+\s*$', cleaned):
        return False
    if re.search(r'\s+\d+\s*$', cleaned) and re.match(r'^\d+\.', cleaned):
        return False

    # Başında opsiyonel bölüm numarası (örn: "7.", "7 -", "VII.") olabilir
    header_pattern = re.compile(
        r'^(?:(?:bölüm|section|chapter)?\s*(?:\d+|[IVXLCDM]+)[\.\-\s:]+)?'
        r'(kaynakça|kaynaklar|references|bibliography|works\s+cited|literatür)\s*:?\s*$',
        re.IGNORECASE
    )
    return bool(header_pattern.match(cleaned))


def is_section_heading_after_bib(line: str) -> bool:
    """Kaynakçadan sonra gelebilecek 'Ekler / Appendices / Özgeçmiş' başlıklarını tespit eder."""
    cleaned = line.strip()
    if not cleaned or len(cleaned) > 45:
        return False
    appendix_pattern = re.compile(
        r'^(?:(?:\d+|[A-Z])[\.\-\s:]+)?(ekler|ek\s*\d+|appendices|appendix|özgeçmiş|cv)\s*:?\s*$',
        re.IGNORECASE
    )
    return bool(appendix_pattern.match(cleaned))


# ---------------------------------------------------------------------------
# 3. KAYNAKÇA SATIRI YAPISAL AYRIŞTIRICI (Hem IEEE [1] hem APA Yazar-Yıl)
# ---------------------------------------------------------------------------

DOI_PATTERN = re.compile(
    r'(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)?(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)',
    re.IGNORECASE
)
URL_PATTERN = re.compile(r'(https?://[^\s<>"]+)', re.IGNORECASE)
PAGE_PATTERN = re.compile(r'(?:pp?\.|s\.|ss\.|pages|sayfa)?\s*(\d{1,5}\s*[-–—]\s*\d{1,5})', re.IGNORECASE)
YEAR_IN_PARENS_PATTERN = re.compile(r'\(\s*((?:19|20)\d{2}[a-z]?)\s*(?:,[^\)]*)?\)')
STANDALONE_YEAR_PATTERN = re.compile(r'(?<![0-9])((?:19|20)\d{2}[a-z]?)(?![0-9])')


def extract_authors_from_segment(author_segment: str) -> list[str]:
    """
    Kaynakça satırının yazar bölümünden (yıl parantezinden veya ilk noktadan önceki kısım)
    yazar soyadlarını (veya kurum adını) çıkarır.
    Örn:
      - "Cohen, W. W., Ravikumar, P., & Fienberg, S. E." -> ["Cohen", "Ravikumar", "Fienberg"]
      - "Jurafsky, D., & Martin, J. H." -> ["Jurafsky", "Martin"]
      - "Ramírez, S." -> ["Ramírez"]
      - "Python Software Foundation." -> ["Python Software Foundation"]
      - "N. T. Taşdemir ve A. Yılmaz" -> ["Taşdemir", "Yılmaz"]
    """
    seg = author_segment.strip()
    if not re.search(r"\b[A-ZÇĞİÖŞÜ]\.$", seg):
        seg = seg.rstrip(".").strip()
    if not seg:
        return []

    seg_clean = re.sub(r'\s+(?:&|and|ve|ile)\s+', ', ', seg, flags=re.IGNORECASE)

    # Durum A: "Soyadı, A. B., Soyadı2, C." formatı (APA klasik)
    apa_surnames = re.findall(
        r'([A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîûA-ZÇĞİÖŞÜ\-\']+),\s*(?:[A-ZÇĞİÖŞÜ]\.\s*)+',
        seg_clean
    )
    if apa_surnames:
        return apa_surnames

    # Durum B: "A. B. Soyadı, C. Soyadı2" formatı (IEEE klasik)
    ieee_surnames = re.findall(
        r'(?:[A-ZÇĞİÖŞÜ]\.\s*)+([A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîûA-ZÇĞİÖŞÜ\-\']+)',
        seg_clean
    )
    if ieee_surnames:
        return ieee_surnames

    # Durum C: Kurumsal yazar veya tek isim (örn: "Crossref", "Python Software Foundation", "SeatGeek")
    first_chunk = seg.split('.')[0].split(',')[0].strip()
    if first_chunk and len(first_chunk) <= 60:
        return [first_chunk]

    return []


def parse_bibliography_line(line: str, auto_index: int) -> dict[str, Any] | None:
    """
    Tek bir kaynakça satırını ayrıştırarak yapısal sözlüğe dönüştürür:
    - Numaralı kaynakça: "[1] ...", "1. ...", "1) ..."
    - Numarasız (APA/MLA) kaynakça: "Cohen, W. W., ... (2003). Başlık..."
    """
    cleaned = line.strip()
    if len(cleaned) < 12:
        return None

    # 1. Numara kontrolü ([1], 1., 1))
    num_match = re.match(r'^(?:\[(\d+)\]|(\d+)[\.\)\-])\s+(.*)$', cleaned)
    if num_match:
        ref_num = int(num_match.group(1) or num_match.group(2))
        content = num_match.group(3).strip()
        is_explicitly_numbered = True
    else:
        ref_num = auto_index
        content = cleaned
        is_explicitly_numbered = False

    # 2. DOI ve URL çıkarımı
    doi_match = DOI_PATTERN.search(content)
    doi = doi_match.group(1).rstrip('.,;)]') if doi_match else None

    url_match = URL_PATTERN.search(content)
    url = url_match.group(1).rstrip('.,;)]') if url_match else None

    # 3. Sayfa numarası çıkarımı (Yıl aralıklarını veya DOI içindeki sayıları sayfa sanmamak için DOI/URL temizlenir)
    content_without_urls = URL_PATTERN.sub('', DOI_PATTERN.sub('', content))
    claimed_pages = None
    for pm in PAGE_PATTERN.finditer(content_without_urls):
        candidate = re.sub(r'\s+', '', pm.group(1)).replace('–', '-').replace('—', '-')
        parts = candidate.split('-')
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            p_start, p_end = int(parts[0]), int(parts[1])
            if not (1900 <= p_start <= 2035 and 1900 <= p_end <= 2035 and (p_end - p_start) <= 30):
                if p_end >= p_start:
                    claimed_pages = f"{p_start}-{p_end}"
                    break

    # 4. Yıl, Yazar ve Başlık çıkarımı
    year: int | None = None
    year_suffix: str = ""
    authors: list[str] = []
    extracted_title: str = ""

    # Önce "(2023)" veya "(2003a)" gibi parantez içi APA yılını ara
    year_parens_match = YEAR_IN_PARENS_PATTERN.search(content_without_urls)
    if year_parens_match:
        raw_year = year_parens_match.group(1)
        year = int(raw_year[:4])
        year_suffix = raw_year[4:]
        author_segment = content_without_urls[:year_parens_match.start()].strip()
        after_year = content_without_urls[year_parens_match.end():].strip().lstrip('.').strip()

        authors = extract_authors_from_segment(author_segment)

        after_year_clean = re.split(
            r'\s*(?:Erişim adresi|Retrieved from|Available at|http|doi:)\s*:?',
            after_year,
            flags=re.IGNORECASE
        )[0].strip()

        # ".docx" gibi bitişik dosya uzantılarında veya "(3rd ed. draft)" gibi parantez içi noktalarda
        # başlığı bölmemek için yalnızca parantez dışındaki ". " (nokta + boşluk) veya satır sonundan böl
        title_match = re.match(r'^((?:[^\.\(]|\([^\)]*\)|\.(?=[a-zA-Z0-9_]))+)', after_year_clean)
        if title_match:
            extracted_title = title_match.group(1).strip().rstrip('.')
        else:
            extracted_title = re.split(r'\.\s+', after_year_clean)[0].strip().rstrip('.')
    else:
        # IEEE / Numaralı format: Tırnak içi başlık ("..." veya “...”) ya da virgül/nokta ayrımı
        quote_title_match = re.search(r'["“”«»]([^"“”«»]{8,250})["“”«»]', content_without_urls)
        year_matches = list(STANDALONE_YEAR_PATTERN.finditer(content_without_urls))
        if year_matches:
            raw_year = year_matches[-1].group(1)
            year = int(raw_year[:4])
            year_suffix = raw_year[4:]

        if quote_title_match:
            extracted_title = quote_title_match.group(1).strip().rstrip(',.')
            author_segment = content_without_urls[:quote_title_match.start()].strip()
            authors = extract_authors_from_segment(author_segment)
        else:
            segments = [s.strip() for s in re.split(r'\.\s+', content_without_urls) if s.strip()]
            if len(segments) >= 2:
                authors = extract_authors_from_segment(segments[0])
                extracted_title = segments[1].rstrip('.')
            elif segments:
                extracted_title = segments[0].rstrip('.')

    first_author = authors[0] if authors else None
    author_year_key = f"{normalize_text(first_author)}_{year}{year_suffix}" if (first_author and year) else None

    return {
        "ref_id": ref_num,
        "is_explicitly_numbered": is_explicitly_numbered,
        "raw_text": cleaned,
        "authors": authors,
        "first_author": first_author,
        "year": year,
        "year_suffix": year_suffix,
        "extracted_title": extracted_title or cleaned,
        "doi": doi,
        "url": url,
        "claimed_pages": claimed_pages,
        "author_year_key": author_year_key
    }


# ---------------------------------------------------------------------------
# 4. METİN İÇİ ATIF VE CÜMLE BAĞLAMI ÇIKARIMI (IEEE + APA + Çapraz Kontrol)
# ---------------------------------------------------------------------------

def extract_citations(text: str) -> set[int]:
    """
    Metin içindeki [1], [2, 3], [4-6], [4–6] gibi numaralı atıfları yakalar.
    - x[0], array[1] gibi matematiksel/programlama dizinlerini dışlar.
    - [2023] gibi tek başına yazılmış yılları dışlar.
    Geriye dönük uyumluluk için set[int] döndürür.
    """
    pattern = r'(?<![a-zA-Z0-9_])\[([\d\s,\-–—]+)\]'
    matches = re.findall(pattern, text)

    citation_ids: set[int] = set()
    for match in matches:
        normalized_match = match.replace('–', '-').replace('—', '-')
        parts = [p.strip() for p in normalized_match.split(',')]
        for part in parts:
            if '-' in part:
                sub = [s.strip() for s in part.split('-')]
                if len(sub) == 2 and sub[0].isdigit() and sub[1].isdigit():
                    start, end = int(sub[0]), int(sub[1])
                    if 0 < start <= 1000 and start <= end <= 1000:
                        citation_ids.update(range(start, end + 1))
            elif part.isdigit():
                val = int(part)
                if 0 < val <= 1000:
                    citation_ids.add(val)

    return citation_ids


# Yazar-Yıl (APA) metin içi atıf kalıpları:
APA_PARENTHETICAL_PATTERN = re.compile(
    r'\(\s*([A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîûA-ZÇĞİÖŞÜ\-\.\s&,;]+?,\s*(?:19|20)\d{2}[a-z]?(?:,\s*(?:s\.|ss\.|p\.|pp\.)\s*\d+(?:-\d+)?)?)\s*\)'
)

APA_NARRATIVE_PATTERN = re.compile(
    r'\b([A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîû\-\']+(?:\s+(?:ve\s+ark\.|vd\.|et\s+al\.|(?:&|ve|and)\s+[A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîû\-\']+))?)\s*\(\s*((?:19|20)\d{2}[a-z]?)\s*\)'
)

PLACEHOLDER_SURNAMES = {"yazar", "author", "surname", "soyadi", "isim", "örnek", "ip"}


def extract_apa_citations(text: str) -> list[dict[str, Any]]:
    """
    Metin içindeki APA/Harvard tarzı (Yazar, Yıl) ve Yazar (Yıl) atıflarını yakalar.
    Örn: "(Cohen, Ravikumar, & Fienberg, 2003)" -> {"first_author": "Cohen", "year": 2003, ...}
    """
    found_apa: list[dict[str, Any]] = []

    # A) Parantez içi çoklu veya tekli APA atıfları: (Cohen et al., 2003; Ramírez, 2023)
    for match in APA_PARENTHETICAL_PATTERN.finditer(text):
        inner = match.group(1)
        sub_citations = [s.strip() for s in inner.split(';') if s.strip()]
        for sub in sub_citations:
            ym = re.search(r',\s*((?:19|20)\d{2})([a-z]?)\b', sub)
            if not ym:
                continue
            year_val = int(ym.group(1))
            year_sfx = ym.group(2)
            author_part = sub[:ym.start()].strip()
            first_word_match = re.match(r'^([A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîûA-ZÇĞİÖŞÜ\-\']+)', author_part)
            if not first_word_match:
                continue
            surname = first_word_match.group(1)
            if normalize_text(surname) in PLACEHOLDER_SURNAMES:
                continue
            found_apa.append({
                "raw_match": match.group(0),
                "first_author": surname,
                "year": year_val,
                "year_suffix": year_sfx,
                "author_year_key": f"{normalize_text(surname)}_{year_val}{year_sfx}",
                "citation_type": "parenthetical_apa"
            })

    # B) Anlatısal (Narrative) APA atıfları: Cohen ve ark. (2003), Ramírez (2023)
    for match in APA_NARRATIVE_PATTERN.finditer(text):
        author_phrase = match.group(1).strip()
        raw_year = match.group(2)
        year_val = int(raw_year[:4])
        year_sfx = raw_year[4:]
        first_word_match = re.match(r'^([A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîûA-ZÇĞİÖŞÜ\-\']+)', author_phrase)
        if not first_word_match:
            continue
        surname = first_word_match.group(1)
        if normalize_text(surname) in PLACEHOLDER_SURNAMES:
            continue
        found_apa.append({
            "raw_match": match.group(0),
            "first_author": surname,
            "year": year_val,
            "year_suffix": year_sfx,
            "author_year_key": f"{normalize_text(surname)}_{year_val}{year_sfx}",
            "citation_type": "narrative_apa"
        })

    return found_apa


def verify_in_text_mentions(body_text: str, bib_entries: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """
    Hocanın istediği 'Metin içinde kaynak geçiyor mu, kaynaktan doğru bahsediliyor mu?'
    kontrolünü cümle bazında yapar:
    1. Her atıfın geçtiği cümleyi (bağlamı) kaydeder.
    2. Cümlede hem numaralı atıf (örn: [6]) hem de yazar/yıl (örn: 'Cohen (2005) ... [6]')
       geçiyorsa, metindeki yazar ve yılın kaynakçadaki [6] numaralı eserle uyuşup uyuşmadığını denetler.
    3. APA tarzı atıfların kaynakçadaki karşılıklarını bulur; yılı hatalı yazılmış olanları
       (örn: metinde 'Cohen (2005)' ama kaynakçada 'Cohen (2003)') yakalar.
    """
    sentences = [s.strip() for s in re.split(r'(?<=[\.\!\?])\s+|\n+', body_text) if s.strip()]

    citation_contexts: list[dict[str, Any]] = []
    mention_mismatches: list[dict[str, Any]] = []
    matched_bib_ids_via_apa: set[int] = set()
    unmatched_apa_citations: list[dict[str, Any]] = []

    bib_by_surname: dict[str, list[dict[str, Any]]] = {}
    for ref_id, entry in bib_entries.items():
        for auth in entry.get("authors", []):
            norm_auth = normalize_text(auth)
            if norm_auth:
                bib_by_surname.setdefault(norm_auth, []).append(entry)

    for sentence in sentences:
        num_citations_in_sent = sorted(list(extract_citations(sentence)))
        apa_citations_in_sent = extract_apa_citations(sentence)
        paired_narrative_checked = False

        # 1) Numaralı atıfların bağlam ve yazar/yıl çapraz kontrolü
        if num_citations_in_sent:
            for ref_num in num_citations_in_sent:
                bib_item = bib_entries.get(ref_num)
                context_record = {
                    "ref_number": ref_num,
                    "sentence": sentence,
                    "status": "OK"
                }

                # Eğer aynı cümlede tek bir numaralı atıf ve anlatısal yazar/yıl varsa çapraz kontrol yap
                if (
                    bib_item
                    and len(num_citations_in_sent) == 1
                    and len(apa_citations_in_sent) == 1
                    and apa_citations_in_sent[0]["citation_type"] == "narrative_apa"
                ):
                    paired_narrative_checked = True
                    claimed_apa = apa_citations_in_sent[0]
                    claimed_surname_norm = normalize_text(claimed_apa["first_author"])
                    claimed_year = claimed_apa["year"]

                    bib_authors_norm = [normalize_text(a) for a in bib_item.get("authors", [])]
                    bib_year = bib_item.get("year")

                    author_ok = any(
                        claimed_surname_norm in ba or ba in claimed_surname_norm
                        for ba in bib_authors_norm if ba
                    ) if bib_authors_norm else True
                    year_ok = (bib_year == claimed_year) if bib_year else True

                    if not author_ok or not year_ok:
                        mismatch_detail = {
                            "type": "NUMBERED_CITATION_MENTION_MISMATCH",
                            "ref_number": ref_num,
                            "sentence": sentence,
                            "claimed_in_text": f"{claimed_apa['first_author']} ({claimed_year})",
                            "actual_in_bibliography": f"{bib_item.get('first_author', 'Bilinmiyor')} ({bib_year})",
                            "message": (
                                f"Metin içinde [{ref_num}] numaralı kaynağa '{claimed_apa['first_author']} ({claimed_year})' "
                                f"olarak atıf yapılmış ancak kaynakçada [{ref_num}] numaralı eser "
                                f"'{bib_item.get('first_author', 'Bilinmiyor')} ({bib_year})' olarak kayıtlı!"
                            )
                        }
                        mention_mismatches.append(mismatch_detail)
                        context_record["status"] = "MISMATCH"
                        context_record["mismatch_detail"] = mismatch_detail["message"]

                citation_contexts.append(context_record)

        # 2) APA (Yazar, Yıl) atıflarının kaynakça ile eşleştirilmesi ve yıl kontrolü
        if paired_narrative_checked:
            continue

        for apa_cit in apa_citations_in_sent:
            surname_norm = normalize_text(apa_cit["first_author"])
            claimed_year = apa_cit["year"]
            candidates = bib_by_surname.get(surname_norm, [])

            if candidates:
                exact_year_matches = [c for c in candidates if c.get("year") == claimed_year]
                if exact_year_matches:
                    for c in exact_year_matches:
                        matched_bib_ids_via_apa.add(c["ref_id"])
                        citation_contexts.append({
                            "ref_number": c["ref_id"],
                            "apa_citation": apa_cit["raw_match"],
                            "sentence": sentence,
                            "status": "OK"
                        })
                else:
                    actual_years = sorted(list({c.get("year") for c in candidates if c.get("year")}))
                    for c in candidates:
                        matched_bib_ids_via_apa.add(c["ref_id"])
                    mismatch_msg = (
                        f"Metin içinde '{apa_cit['raw_match']}' olarak bahsedilmiş ancak kaynakçada "
                        f"'{apa_cit['first_author']}' yazarının eseri {actual_years} yılına ait!"
                    )
                    mention_mismatches.append({
                        "type": "APA_YEAR_MISMATCH",
                        "sentence": sentence,
                        "claimed_in_text": apa_cit["raw_match"],
                        "actual_years_in_bibliography": actual_years,
                        "message": mismatch_msg
                    })
            else:
                unmatched_apa_citations.append({
                    "raw_match": apa_cit["raw_match"],
                    "first_author": apa_cit["first_author"],
                    "year": claimed_year,
                    "sentence": sentence
                })

    return {
        "citation_contexts": citation_contexts,
        "mention_mismatches": mention_mismatches,
        "matched_bib_ids_via_apa": matched_bib_ids_via_apa,
        "unmatched_apa_citations": unmatched_apa_citations
    }


# ---------------------------------------------------------------------------
# 5. ANA BÖLÜM AYRIŞTIRICI (Geriye Dönük Uyumlu + Gelişmiş Analiz)
# ---------------------------------------------------------------------------

def parse_document_sections(paragraphs: list[str]):
    """
    Belgeyi Ana Metin (body_text) ve Kaynakça (bib_entries) olarak ayırır.
    - 'Kaynakça yönetimi...' gibi cümle başlangıçlarını ve İçindekiler (TOC) satırlarını
      yanlışlıkla Kaynakça başlığı sanmaz; belgedeki SON gerçek Kaynakça başlığını baz alır.
    - Hem numaralı ([1], 1.) hem de APA (Yazar, Yıl) kaynakça satırlarını ayrıştırır.
    - Mükerrer kaynakları (aynı numara, aynı DOI veya aynı başlık) tespit eder.
    """
    header_indices = [i for i, p in enumerate(paragraphs) if is_bibliography_header(p)]

    if header_indices:
        bib_start_idx = header_indices[-1]
        body_text_parts = [p.strip() for p in paragraphs[:bib_start_idx] if p.strip()]

        bibliography_lines: list[str] = []
        for p in paragraphs[bib_start_idx + 1:]:
            cleaned = p.strip()
            if not cleaned:
                continue
            if is_section_heading_after_bib(cleaned):
                break
            bibliography_lines.append(cleaned)
    else:
        body_text_parts = [p.strip() for p in paragraphs if p.strip()]
        bibliography_lines = []

    body_text = "\n".join(body_text_parts)

    bib_entries: dict[int, dict[str, Any]] = {}
    duplicate_refs: list[Any] = []
    seen_dois: dict[str, int] = {}
    seen_titles: dict[str, int] = {}

    auto_idx = 1
    for line in bibliography_lines:
        parsed = parse_bibliography_line(line, auto_idx)
        if not parsed:
            continue

        ref_num = parsed["ref_id"]

        # 1) Aynı referans numarası mükerrerliği
        if ref_num in bib_entries:
            duplicate_refs.append({
                "type": "DUPLICATE_NUMBER",
                "ref_id": ref_num,
                "message": f"[{ref_num}] numarası kaynakçada birden fazla kez kullanılmış."
            })
            while ref_num in bib_entries:
                ref_num += 1000
            parsed["ref_id"] = ref_num

        # 2) Aynı DOI mükerrerliği
        doi_val = parsed.get("doi")
        if doi_val:
            doi_lower = doi_val.lower()
            if doi_lower in seen_dois:
                duplicate_refs.append({
                    "type": "DUPLICATE_DOI",
                    "ref_id": ref_num,
                    "duplicate_of": seen_dois[doi_lower],
                    "doi": doi_val,
                    "message": f"Kaynak [{ref_num}] ile Kaynak [{seen_dois[doi_lower]}] aynı DOI'ye ({doi_val}) sahip."
                })
            else:
                seen_dois[doi_lower] = ref_num

        # 3) Aynı başlık mükerrerliği
        norm_title = normalize_text(parsed.get("extracted_title", ""))
        if len(norm_title) >= 20:
            if norm_title in seen_titles:
                duplicate_refs.append({
                    "type": "DUPLICATE_TITLE",
                    "ref_id": ref_num,
                    "duplicate_of": seen_titles[norm_title],
                    "title": parsed.get("extracted_title"),
                    "message": f"Kaynak [{ref_num}] ile Kaynak [{seen_titles[norm_title]}] aynı başlığa sahip."
                })
            else:
                seen_titles[norm_title] = ref_num

        bib_entries[ref_num] = parsed
        auto_idx += 1

    return body_text, bib_entries, duplicate_refs