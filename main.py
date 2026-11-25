from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
import io
import re
import asyncio
import time
import os
import docx
import httpx

try:
    from app.parsers.citation_regex import (
        extract_all_paragraphs,
        extract_paragraphs_from_pdf,
        extract_citations,
        extract_apa_citations,
        parse_document_sections,
        verify_in_text_mentions,
    )
    from app.services.crossref_service import (
        verify_reference_with_crossref,
        search_by_title_and_author,
        compute_title_similarity,
    )
except ImportError:
    from citation_regex import (
        extract_all_paragraphs,
        extract_paragraphs_from_pdf,
        extract_citations,
        extract_apa_citations,
        parse_document_sections,
        verify_in_text_mentions,
    )
    from crossref_service import (
        verify_reference_with_crossref,
        search_by_title_and_author,
        compute_title_similarity,
    )

API_HEADERS = {
    "User-Agent": "AcademicCitationVerifier/2.0 (mailto:tuana@nku.edu.tr; Tekirdag Namik Kemal University - Computer Engineering)"
}

app = FastAPI(
    title="Akademik Kaynakça Doğrulama ve Metin İçi Atıf Tutarlılığı Sistemi",
    version="2.0.0",
    description=(
        "Hem numaralı ([1]) hem APA (Yazar, Yıl) atıfları yakalayan, "
        "DOI'siz hakemli dergi yayınlarını 7 Küresel Akademik Veri Tabanı "
        "(Crossref, OpenAlex, DBLP Computer Science, Europe PMC, arXiv, DataCite, Semantic Scholar) "
        "üzerinden Başlık + Yazar + Yıl ile doğrulayan ve metin içi doğru bahsedilmeyi denetleyen API."
    )
)

# ---------------------------------------------------------------------------
# CORS VE ÖZEL AĞ ERİŞİMİ (Private Network Access) YAPILANDIRMASI
# ---------------------------------------------------------------------------
# allow_credentials=False olmalıdır; çünkü allow_origins=["*"] ile allow_credentials=True
# aynı anda kullanıldığında tarayıcılar (Chrome/Edge/Firefox) CORS hatası ("Failed to fetch") verir.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_private_network_headers(request, call_next):
    """
    Chrome ve Edge'in yerel ağ (localhost / 127.0.0.1) güvenlik politikası olan
    Private Network Access (PNA) preflight kontrollerini sorunsuz geçmesini sağlar.
    """
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# ---------------------------------------------------------------------------
# STATİK SAYFA VE DURUM ENDPOINTLERİ
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    """
    Kullanıcı tarayıcıda http://127.0.0.1:8000 açtığında doğrudan index.html arayüzünü sunar.
    Böylece dosya protokolü (file://) kaynaklı tüm CORS engelleri tamamen ortadan kalkar.
    """
    candidates = [
        "index.html",
        os.path.join(os.path.dirname(__file__), "index.html"),
        os.path.join(os.path.dirname(__file__), "..", "index.html"),
        "templates/index.html",
        "static/index.html"
    ]
    for path in candidates:
        if os.path.exists(path):
            return FileResponse(path, media_type="text/html")

    return {
        "status": "Sistem aktif",
        "version": "2.0.0",
        "message": "index.html dosyasını proje dizinine koyduğunuzda bu adreste doğrudan web arayüzü açılacaktır.",
        "supported_databases": [
            "Crossref (DOI ve Bibliyografik)",
            "OpenAlex (Açık Bilim ve Hakemli Makaleler)",
            "DBLP (Bilgisayar Mühendisliği ve IEEE/ACM Bildirileri)",
            "Europe PMC (Açık Erişim ve Çok Disiplinli Fen/Mühendislik)",
            "arXiv (Yapay Zekâ ve Bilgisayar Bilimi Önbasımları)",
            "DataCite (Veri Kümeleri ve Araştırma Çıktıları)",
            "Semantic Scholar (AI Destekli Akademik Literatür Grafı)"
        ]
    }


@app.get("/status")
def status_endpoint():
    return {
        "status": "Sistem aktif",
        "version": "2.0.0",
        "supported_databases": [
            "Crossref",
            "OpenAlex",
            "DBLP",
            "Europe PMC",
            "arXiv",
            "DataCite",
            "Semantic Scholar"
        ]
    }


# ---------------------------------------------------------------------------
# ANA ANALİZ ENDPOINTİ (.docx & .pdf)
# ---------------------------------------------------------------------------

@app.post("/analyze")
async def analyze_document(
    file: UploadFile = File(...),
    similarity_threshold: int = Query(80, ge=50, le=100, description="Başlık benzerlik eşiği (%)")
):
    filename_lower = (file.filename or "").lower()
    if not (filename_lower.endswith(".docx") or filename_lower.endswith(".pdf")):
        raise HTTPException(
            status_code=400,
            detail="Lütfen geçerli bir .docx veya .pdf dosyası yükleyin."
        )

    start_time = time.time()

    try:
        contents = await file.read()

        # 1. Dosya formatına göre (.pdf veya .docx) paragrafları eksiksiz oku
        if filename_lower.endswith(".pdf"):
            import unicodedata
            try:
                import fitz  # PyMuPDF
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="PDF analizi için PyMuPDF kurulu değil. Terminalde 'pip install pymupdf' çalıştırın."
                )

            pdf_doc = fitz.open(stream=contents, filetype="pdf")
            raw_paragraphs: list[str] = []

            bib_header_re = re.compile(
                r'^(?:(?:bölüm|section|chapter)?\s*(?:\d+|[IVXLCDM]+)[\.\-\s:]+)?'
                r'(kaynakça|kaynaklar|references|bibliography|works\s+cited|literatür)\s*:?\s*$',
                re.IGNORECASE
            )
            appendix_header_re = re.compile(
                r'^(?:(?:\d+|[A-Z])[\.\-\s:]+)?(ekler|ek\s*\d+|appendices|appendix|özgeçmiş|cv)\s*:?\s*$',
                re.IGNORECASE
            )

            for page in pdf_doc:
                page_height = page.rect.height
                blocks = page.get_text("blocks", sort=True)

                for b in blocks:
                    if len(b) >= 7 and b[6] != 0:
                        continue
                    x0, y0, x1, y1, raw_text = b[0], b[1], b[2], b[3], b[4]
                    if not raw_text or not raw_text.strip():
                        continue

                    # Üst/alt bilgi ve sayfa numarası gürültüsünü atla
                    if (y0 < page_height * 0.04 or y1 > page_height * 0.96) and len(raw_text.strip()) < 65:
                        if re.match(r'^[\d\s\-–—/]+$', raw_text.strip()) or "page" in raw_text.lower() or "sayfa" in raw_text.lower():
                            continue

                    cleaned_block = unicodedata.normalize("NFKC", raw_text)
                    cleaned_block = re.sub(r'([a-zçğıöşüA-ZÇĞİÖŞÜ])-\s*\n\s*([a-zçğıöşü])', r'\1\2', cleaned_block)

                    for line in [l.strip() for l in cleaned_block.splitlines() if l.strip()]:
                        if re.match(r'^\d{1,4}$', line):
                            continue
                        raw_paragraphs.append(line)

            pdf_doc.close()

            # PDF'te Kaynakça başlığından sonraki alt satırlara bölünmüş kaynakları tek satırda birleştir
            bib_indices = [
                i for i, p in enumerate(raw_paragraphs)
                if len(p) <= 45
                and not re.search(r'(\.{2,}|\t+)\s*\d+\s*$', p)
                and not (re.search(r'\s+\d+\s*$', p) and re.match(r'^\d+\.', p))
                and bib_header_re.match(p)
            ]

            if bib_indices:
                b_idx = bib_indices[-1]
                body_lines = raw_paragraphs[:b_idx + 1]
                after_bib_lines = raw_paragraphs[b_idx + 1:]

                numbered_start_re = re.compile(r'^(?:\[\d+\]|\d{1,3}[\.\)\-])\s+[A-ZÇĞİÖŞÜÂÎÛ"\'“]')
                apa_start_re = re.compile(
                    r'^[A-ZÇĞİÖŞÜÂÎÛ][a-zçğıöşüâîûA-ZÇĞİÖŞÜ\-\']+,\s*(?:[A-ZÇĞİÖŞÜ]\.|[A-ZÇĞİÖŞÜ][a-zçğıöşü]+)'
                )
                year_parens_re = re.compile(r'\(\s*(?:19|20)\d{2}[a-z]?\s*\)')

                num_count = sum(1 for l in after_bib_lines if numbered_start_re.match(l))
                mostly_numbered = num_count >= max(1, len(after_bib_lines) // 6)

                merged_bib: list[str] = []
                tail_lines: list[str] = []
                in_appendix = False

                for line in after_bib_lines:
                    if len(line) <= 45 and appendix_header_re.match(line):
                        in_appendix = True
                    if in_appendix:
                        tail_lines.append(line)
                        continue

                    is_new = False
                    if mostly_numbered:
                        if numbered_start_re.match(line):
                            is_new = True
                    else:
                        if (
                            numbered_start_re.match(line)
                            or apa_start_re.match(line)
                            or (year_parens_re.search(line[:75]) and line[0].isupper())
                        ):
                            is_new = True

                    if is_new or not merged_bib:
                        merged_bib.append(line)
                    else:
                        if merged_bib[-1].endswith(("/", "-", "_")) and (
                            "http" in merged_bib[-1].lower() or "10." in merged_bib[-1] or "doi" in merged_bib[-1].lower()
                        ):
                            merged_bib[-1] = f"{merged_bib[-1]}{line}"
                        else:
                            merged_bib[-1] = f"{merged_bib[-1]} {line}"

                paragraphs = body_lines + merged_bib + tail_lines
            else:
                paragraphs = raw_paragraphs
        else:
            doc = docx.Document(io.BytesIO(contents))
            paragraphs = extract_all_paragraphs(doc)

        if not paragraphs:
            raise HTTPException(
                status_code=422,
                detail="Belgeden metin okunamadı. PDF dosyası taranmış görüntü (OCR'sız) olabilir."
            )

        # 2. Ana Metin ve Kaynakça bölümlerini ayrıştır
        body_text, bib_entries, duplicate_refs = parse_document_sections(paragraphs)

        # 3. Metin içi numaralı ([1]) ve APA ((Yazar, Yıl)) atıfları + doğru bahsedilme kontrolünü çalıştır
        cited_numbers = extract_citations(body_text)
        apa_citations = extract_apa_citations(body_text)
        mention_analysis = verify_in_text_mentions(body_text, bib_entries)

        # Hem numaralı hem APA üzerinden eşleşen tüm kaynakça ID'leri
        all_cited_bib_ids = cited_numbers.union(mention_analysis["matched_bib_ids_via_apa"])
        bib_keys = set(bib_entries.keys())

        missing_numbered_in_bib = sorted(list(cited_numbers - bib_keys))
        missing_apa_in_bib = mention_analysis["unmatched_apa_citations"]
        unused_in_text = sorted(list(bib_keys - all_cited_bib_ids))

        # Her kaynak için metin içinde geçtiği cümleleri grupla
        contexts_by_ref: dict[int, list[str]] = {}
        for ctx in mention_analysis["citation_contexts"]:
            r_id = ctx.get("ref_number")
            sent = ctx.get("sentence")
            if r_id and sent:
                contexts_by_ref.setdefault(r_id, []).append(sent)

        # 4. Asenkron (Eşzamanlı) Çoklu API Doğrulaması (7 Akademik Veri Tabanı)
        tasks = [
            verify_reference_with_crossref(
                ref_info=info,
                similarity_threshold=similarity_threshold,
                context_sentences=contexts_by_ref.get(ref_id, [])
            )
            for ref_id, info in bib_entries.items()
        ]
        verification_results = await asyncio.gather(*tasks)

        verified_references = []
        status_counts: dict[str, int] = {}

        for (ref_id, info), ver_res in zip(bib_entries.items(), verification_results):
            st = ver_res.get("status", "UNKNOWN")
            status_counts[st] = status_counts.get(st, 0) + 1

            verified_references.append({
                "ref_number": ref_id,
                "raw_citation": info["raw_text"],
                "extracted_authors": info.get("authors", []),
                "extracted_year": info.get("year"),
                "extracted_title": info.get("extracted_title"),
                "doi_in_document": info.get("doi"),
                "url_in_document": info.get("url"),
                "claimed_pages": info.get("claimed_pages"),
                "in_text_mention_count": len(contexts_by_ref.get(ref_id, [])),
                "verification": ver_res
            })

        elapsed = round(time.time() - start_time, 2)
        mention_mismatches = mention_analysis["mention_mismatches"]

        is_consistent = (
            len(missing_numbered_in_bib) == 0
            and len(missing_apa_in_bib) == 0
            and len(unused_in_text) == 0
            and len(duplicate_refs) == 0
            and len(mention_mismatches) == 0
        )

        return {
            "analysis_summary": {
                "filename": file.filename,
                "processing_time_seconds": elapsed,
                "total_blocks_read": len(paragraphs),
                "total_references_found": len(bib_entries),
                "similarity_threshold_used": f"%{similarity_threshold}",
                "verification_breakdown": status_counts,
                "academic_databases_queried": [
                    "Crossref",
                    "OpenAlex",
                    "DBLP",
                    "Europe PMC",
                    "arXiv",
                    "DataCite",
                    "Semantic Scholar"
                ]
            },
            "citation_consistency": {
                "numbered_citations_in_body": sorted(list(cited_numbers)),
                "apa_citations_in_body_count": len(apa_citations),
                "references_in_bibliography": sorted(list(bib_keys)),
                "missing_numbered_in_bibliography": missing_numbered_in_bib,
                "missing_apa_in_bibliography": missing_apa_in_bib,
                "unused_in_text": unused_in_text,
                "duplicate_entries_in_bibliography": duplicate_refs,
                "in_text_mention_mismatches": mention_mismatches,
                "is_perfectly_consistent": is_consistent
            },
            "bibliographic_verification": verified_references
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"İşlem hatası: {str(e)}")


# ---------------------------------------------------------------------------
# İSİMLE (BAŞLIK / YAZAR) MAKALE ARAMA ENDPOINTİ
# ---------------------------------------------------------------------------

@app.get("/search")
async def search_articles_by_name(
    query: str = Query(..., min_length=6, description="Aranacak makale adı veya başlığı"),
    author: str | None = Query(None, description="Opsiyonel yazar soyadı"),
    year: int | None = Query(None, description="Opsiyonel yayın yılı")
):
    start_time = time.time()
    try:
        candidates = await search_by_title_and_author(
            title=query,
            first_author=author.strip() if author else None,
            year=year
        )

        seen_keys: set[str] = set()
        results = []

        scored_candidates = sorted(
            candidates,
            key=lambda c: compute_title_similarity(query, c.get("title", "")),
            reverse=True
        )

        for cand in scored_candidates:
            title = (cand.get("title") or "").strip()
            if not title:
                continue

            doi = (cand.get("doi") or "").strip().lower()
            dedup_key = f"doi:{doi}" if doi else f"title:{title.lower()}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            sim = compute_title_similarity(query, title)
            results.append({
                "title": title,
                "authors": cand.get("authors") or [],
                "year": cand.get("year"),
                "journal": cand.get("journal") or "",
                "doi": cand.get("doi"),
                "url": cand.get("url") or (f"https://doi.org/{cand['doi']}" if cand.get("doi") else None),
                "source_database": cand.get("source_api", "Bilinmiyor"),
                "similarity_percent": sim
            })

        return {
            "query": query,
            "author": author,
            "year": year,
            "total_results": len(results),
            "processing_time_seconds": round(time.time() - start_time, 2),
            "results": results[:15]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Arama hatası: {str(e)}")


# ---------------------------------------------------------------------------
# BULUNAN MAKALENİN KAYNAKÇASINI ÇEKİP TEK TEK DOĞRULAMA ENDPOINTİ
# ---------------------------------------------------------------------------

def _parse_year_safe(value) -> int | None:
    if value is None:
        return None
    m = re.search(r'(19|20)\d{2}', str(value))
    return int(m.group(0)) if m else None


async def _fetch_article_references(
    doi: str | None,
    title: str | None,
    max_refs: int = 25
) -> tuple[str, list[dict]]:
    """
    Seçilen makalenin kaynakça listesini sırasıyla Crossref, OpenAlex ve Semantic Scholar
    üzerinden çeker ve verify_reference_with_crossref uyumlu formata dönüştürür.
    """
    clean_doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', (doi or '').strip(), flags=re.IGNORECASE)
    extracted_refs: list[dict] = []

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        # 1. Crossref üzerinden kaynakça çekme (en zengin yapısal kaynakça listesi)
        if clean_doi:
            try:
                resp = await client.get(f"https://api.crossref.org/works/{clean_doi}", headers=API_HEADERS)
                if resp.status_code == 200:
                    raw_refs = (resp.json().get("message") or {}).get("reference") or []
                    for r in raw_refs[:max_refs]:
                        ref_doi = r.get("DOI")
                        ref_title = r.get("article-title") or r.get("volume-title") or r.get("series-title") or ""
                        ref_author = (r.get("author") or "").strip()
                        ref_year = _parse_year_safe(r.get("year"))
                        ref_journal = r.get("journal-title") or ""
                        ref_page = r.get("first-page")
                        unstructured = (r.get("unstructured") or "").strip()

                        if unstructured:
                            raw_text = unstructured
                        else:
                            parts = [p for p in [ref_author, f"({ref_year})" if ref_year else "", ref_title, ref_journal, f"DOI: {ref_doi}" if ref_doi else ""] if p]
                            raw_text = " ".join(parts).strip()

                        if not raw_text and not ref_doi:
                            continue

                        extracted_refs.append({
                            "raw_text": raw_text or f"DOI: {ref_doi}",
                            "extracted_title": ref_title or raw_text,
                            "authors": [ref_author] if ref_author else [],
                            "first_author": ref_author or None,
                            "year": ref_year,
                            "doi": ref_doi,
                            "url": None,
                            "claimed_pages": ref_page
                        })
                    if extracted_refs:
                        return "Crossref", extracted_refs
            except Exception:
                pass

        # 2. OpenAlex üzerinden kaynakça (referenced_works) çekme
        try:
            work_data = None
            if clean_doi:
                resp = await client.get(f"https://api.openalex.org/works/https://doi.org/{clean_doi}", headers=API_HEADERS)
                if resp.status_code == 200:
                    work_data = resp.json()
            elif title:
                resp = await client.get(
                    "https://api.openalex.org/works",
                    params={"search": title.strip(), "per-page": 1},
                    headers=API_HEADERS
                )
                if resp.status_code == 200:
                    res_list = resp.json().get("results") or []
                    if res_list:
                        work_data = res_list[0]

            referenced_urls = (work_data or {}).get("referenced_works") or []
            if referenced_urls:
                work_ids = [u.rstrip("/").split("/")[-1] for u in referenced_urls[:max_refs] if "/" in u]
                if work_ids:
                    batch_resp = await client.get(
                        "https://api.openalex.org/works",
                        params={"filter": f"openalex_id:{'|'.join(work_ids)}", "per-page": len(work_ids)},
                        headers=API_HEADERS
                    )
                    if batch_resp.status_code == 200:
                        for w in batch_resp.json().get("results") or []:
                            w_title = (w.get("title") or w.get("display_name") or "").strip()
                            w_authors = []
                            for auth in w.get("authorships") or []:
                                name = (auth.get("author") or {}).get("display_name") or ""
                                if name:
                                    w_authors.append(name.strip().split()[-1])
                            w_year = w.get("publication_year")
                            raw_w_doi = w.get("doi") or ""
                            w_doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', raw_w_doi, flags=re.IGNORECASE) or None
                            journal = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""

                            parts = [p for p in [", ".join(w_authors[:3]), f"({w_year})" if w_year else "", w_title, journal] if p]
                            raw_text = ". ".join(parts)

                            if w_title or w_doi:
                                extracted_refs.append({
                                    "raw_text": raw_text or w_title or f"DOI: {w_doi}",
                                    "extracted_title": w_title or raw_text,
                                    "authors": w_authors,
                                    "first_author": w_authors[0] if w_authors else None,
                                    "year": int(w_year) if w_year else None,
                                    "doi": w_doi,
                                    "url": w.get("id"),
                                    "claimed_pages": None
                                })
                        if extracted_refs:
                            return "OpenAlex", extracted_refs
        except Exception:
            pass

        # 3. Semantic Scholar üzerinden kaynakça çekme (yedek)
        if clean_doi:
            try:
                s2_resp = await client.get(
                    f"https://api.semanticscholar.org/graph/v1/paper/DOI:{clean_doi}/references",
                    params={"fields": "title,authors,year,externalIds,venue", "limit": max_refs},
                    headers=API_HEADERS
                )
                if s2_resp.status_code == 200:
                    for entry in s2_resp.json().get("data") or []:
                        cited = entry.get("citedPaper") or {}
                        c_title = (cited.get("title") or "").strip()
                        c_authors = [a.get("name", "").strip().split()[-1] for a in (cited.get("authors") or []) if a.get("name")]
                        c_year = cited.get("year")
                        c_doi = (cited.get("externalIds") or {}).get("DOI")
                        c_venue = cited.get("venue") or ""
                        parts = [p for p in [", ".join(c_authors[:3]), f"({c_year})" if c_year else "", c_title, c_venue] if p]
                        raw_text = ". ".join(parts)
                        if c_title or c_doi:
                            extracted_refs.append({
                                "raw_text": raw_text or c_title or f"DOI: {c_doi}",
                                "extracted_title": c_title or raw_text,
                                "authors": c_authors,
                                "first_author": c_authors[0] if c_authors else None,
                                "year": int(c_year) if c_year else None,
                                "doi": c_doi,
                                "url": None,
                                "claimed_pages": None
                            })
                    if extracted_refs:
                        return "Semantic Scholar", extracted_refs
            except Exception:
                pass

    return "Yok", []


@app.get("/analyze-article")
async def analyze_article_references(
    doi: str | None = Query(None, description="İncelenecek makalenin DOI numarası"),
    title: str | None = Query(None, description="İncelenecek makalenin başlığı"),
    max_refs: int = Query(25, ge=5, le=50, description="Taranacak maksimum kaynak sayısı"),
    similarity_threshold: int = Query(80, ge=50, le=100, description="Başlık benzerlik eşiği (%)")
):
    if not doi and not title:
        raise HTTPException(
            status_code=400,
            detail="Kaynakça taraması için en azından DOI veya makale başlığı gereklidir."
        )

    start_time = time.time()
    try:
        ref_source, refs_list = await _fetch_article_references(doi=doi, title=title, max_refs=max_refs)

        if not refs_list:
            raise HTTPException(
                status_code=404,
                detail="Bu makalenin açık kaynakça (reference) listesine Crossref, OpenAlex ve Semantic Scholar üzerinden erişilemedi."
            )

        tasks = [
            verify_reference_with_crossref(
                ref_info=info,
                similarity_threshold=similarity_threshold,
                context_sentences=[]
            )
            for info in refs_list
        ]
        verification_results = await asyncio.gather(*tasks)

        verified_references = []
        status_counts: dict[str, int] = {}

        for idx, (info, ver_res) in enumerate(zip(refs_list, verification_results), start=1):
            st = ver_res.get("status", "UNKNOWN")
            status_counts[st] = status_counts.get(st, 0) + 1

            verified_references.append({
                "ref_number": idx,
                "raw_citation": info["raw_text"],
                "extracted_authors": info.get("authors", []),
                "extracted_year": info.get("year"),
                "extracted_title": info.get("extracted_title"),
                "doi_in_document": info.get("doi"),
                "url_in_document": info.get("url"),
                "claimed_pages": info.get("claimed_pages"),
                "in_text_mention_count": 0,
                "verification": ver_res
            })

        elapsed = round(time.time() - start_time, 2)

        return {
            "source_mode": "article_reference_scan",
            "article_info": {
                "title": title or doi,
                "doi": doi,
                "reference_list_source": ref_source
            },
            "analysis_summary": {
                "filename": title or doi,
                "processing_time_seconds": elapsed,
                "total_references_found": len(refs_list),
                "similarity_threshold_used": f"%{similarity_threshold}",
                "verification_breakdown": status_counts,
                "academic_databases_queried": [
                    "Crossref",
                    "OpenAlex",
                    "DBLP",
                    "Europe PMC",
                    "arXiv",
                    "DataCite",
                    "Semantic Scholar"
                ]
            },
            "citation_consistency": {
                "numbered_citations_in_body": [],
                "apa_citations_in_body_count": 0,
                "references_in_bibliography": list(range(1, len(refs_list) + 1)),
                "missing_numbered_in_bibliography": [],
                "missing_apa_in_bibliography": [],
                "unused_in_text": [],
                "duplicate_entries_in_bibliography": [],
                "in_text_mention_mismatches": [],
                "is_perfectly_consistent": True
            },
            "bibliographic_verification": verified_references
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Makale kaynakça tarama hatası: {str(e)}")