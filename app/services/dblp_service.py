import re
from typing import Any
import httpx

DEFAULT_HEADERS = {
    "User-Agent": "AcademicCitationVerifier/2.0 (mailto:tuana@nku.edu.tr; Tekirdag Namik Kemal University - Computer Engineering)"
}


def standardize_dblp_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    DBLP Computer Science Bibliography API yanıtını standart doğrulama şemasına dönüştürür.
    Bilgisayar Mühendisliği alanındaki IEEE, ACM, Springer konferans ve dergilerinde altın standarttır.
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


async def search_dblp(client: httpx.AsyncClient, title: str, max_results: int = 4) -> list[dict[str, Any]]:
    """DBLP üzerinden makale ve konferans bildirisi araması yapar."""
    clean_title = title.strip()
    if len(clean_title) < 5:
        return []

    try:
        resp = await client.get(
            "https://dblp.org/search/publ/api",
            params={"q": clean_title, "format": "json", "h": max_results},
            headers=DEFAULT_HEADERS
        )
        if resp.status_code == 200:
            data = resp.json()
            hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
            return [standardize_dblp_item(it) for it in hits]
    except Exception:
        pass
    return []