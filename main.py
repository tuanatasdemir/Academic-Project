from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import io
import asyncio
import time
import docx

from app.parsers.citation_regex import (
    extract_all_paragraphs,
    extract_citations,
    extract_apa_citations,
    parse_document_sections,
    verify_in_text_mentions,
)
from app.services.crossref_service import verify_reference_with_crossref

app = FastAPI(
    title="Akademik Kaynakça Doğrulama ve Metin İçi Atıf Tutarlılığı Sistemi",
    version="1.0.0",
    description=(
        "Hem numaralı ([1]) hem APA (Yazar, Yıl) atıfları yakalayan, "
        "DOI'siz hakemli dergi yayınlarını Crossref, OpenAlex, DataCite ve Semantic Scholar "
        "üzerinden Başlık + Yazar + Yıl ile doğrulayan ve metin içi doğru bahsedilmeyi denetleyen API."
    )
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "status": "Sistem aktif",
        "version": "1.0.0",
        "features": [
            "IEEE ([1]) ve APA (Yazar, Yıl) Hibrit Atıf Ayrıştırma",
            "Tablo Hücreleri ve Paragraf Okuma",
            "Crossref + OpenAlex + DataCite + Semantic Scholar Çoklu API Doğrulaması",
            "DOI Olmayan Hakemli Yayınlar İçin Başlık, Yazar ve Yıldan Doğrulama",
            "Metin İçi Yazar/Yıl Çapraz Kontrolü ve Bağlam Analizi"
        ]
    }


@app.post("/analyze")
async def analyze_document(
    file: UploadFile = File(...),
    similarity_threshold: int = Query(80, ge=50, le=100, description="Başlık benzerlik eşiği (%)")
):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Lütfen geçerli bir .docx dosyası yükleyin.")

    start_time = time.time()

    try:
        contents = await file.read()
        doc = docx.Document(io.BytesIO(contents))

        # 1. Paragrafları ve tablo hücrelerini eksiksiz oku
        paragraphs = extract_all_paragraphs(doc)

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

        # 4. Asenkron (Eşzamanlı) Çoklu API Doğrulaması (DOI + Başlık + Yazar + Yıl)
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
                "verification_breakdown": status_counts
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"İşlem hatası: {str(e)}")