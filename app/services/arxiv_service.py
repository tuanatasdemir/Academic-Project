import re
import xml.etree.ElementTree as ET
from typing import Any
import httpx

DEFAULT_HEADERS = {
    "User-Agent": "AcademicCitationVerifier/2.0 (mailto:tuana@nku.edu.tr; Tekirdag Namik Kemal University - Computer Engineering)"
}


def parse_arxiv_atom(xml_text: str) -> list[dict[str, Any]]:
    """
    arXiv Atom XML API yanıtını standart doğrulama şemasına dönüştürür.
    Yapay zekâ ve bilgisayar bilimlerindeki en güncel önbasımları yakalar.
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


async def search_arxiv(client: httpx.AsyncClient, title: str, max_results: int = 3) -> list[dict[str, Any]]:
    """arXiv üzerinde başlık ve anahtar kelime araması yapar."""
    clean_title = title.strip()
    if len(clean_title) < 5:
        return []

    try:
        resp = await client.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": f"all:{clean_title}", "max_results": max_results},
            headers=DEFAULT_HEADERS
        )
        if resp.status_code == 200 and resp.text:
            return parse_arxiv_atom(resp.text)
    except Exception:
        pass
    return []