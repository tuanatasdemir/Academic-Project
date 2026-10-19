import re
from typing import Any
import httpx

DEFAULT_HEADERS = {
    "User-Agent": "AcademicCitationVerifier/2.0 (mailto:tuana@nku.edu.tr; Tekirdag Namik Kemal University - Computer Engineering)"
}


def standardize_europepmc_item(item: dict[str, Any]) -> dict[str, Any]:
    """
    Europe PMC REST API yanıtını standart doğrulama şemasına dönüştürür.
    Açık erişimli fen, mühendislik ve tıp literatüründe dünya lideridir.
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


async def search_europepmc(client: httpx.AsyncClient, title: str, max_results: int = 4) -> list[dict[str, Any]]:
    """Europe PMC üzerinde başlık araması yapar."""
    clean_title = title.strip()
    if len(clean_title) < 5:
        return []

    try:
        resp = await client.get(
            "https://europepmc.org/webservices/rest/search",
            params={"query": f"TITLE:\"{clean_title}\"", "format": "json", "pageSize": max_results},
            headers=DEFAULT_HEADERS
        )
        if resp.status_code == 200:
            data = resp.json()
            results = (data.get("resultList") or {}).get("result") or []
            return [standardize_europepmc_item(it) for it in results]
    except Exception:
        pass
    return []


async def fetch_europepmc_by_doi(client: httpx.AsyncClient, doi: str) -> dict[str, Any] | None:
    """Europe PMC üzerinde doğrudan DOI ile sorgulama yapar."""
    try:
        resp = await client.get(
            "https://europepmc.org/webservices/rest/search",
            params={"query": f"DOI:\"{doi}\"", "format": "json", "pageSize": 1},
            headers=DEFAULT_HEADERS
        )
        if resp.status_code == 200:
            data = resp.json()
            results = (data.get("resultList") or {}).get("result") or []
            if results:
                return standardize_europepmc_item(results[0])
    except Exception:
        pass
    return None