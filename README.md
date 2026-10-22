# Academic-Project

A system that checks the consistency of in-text citations in academic documents (.docx) and verifies the works listed in the bibliography against Crossref and OpenAlex using DOI, title, and author information.

Project status: work in progress. See [Project Status](#project-status) for what is implemented and what is planned.

## Problem

Reference management in theses and papers is error-prone. Tools such as Mendeley and Zotero work well while writing, but the link between citations and the bibliography breaks when sources are added or deleted by hand, when sections from different files are merged, or when a document is converted to plain text.

Typical errors that are easy to miss in a long document:

- A citation in the text with no matching bibliography entry
- A bibliography entry that is never cited in the text
- The same source listed twice
- A DOI that belongs to a different publication than the one written in the bibliography

Existing tools cover only part of this problem. Crossref Simple Text Query finds DOIs for a list of references, but it does not read the document body or compare citations with the bibliography. This project checks both sides of a finished document and validates the bibliography against external databases.

## What the system does

1. Accepts a .docx file and separates the main text from the "Kaynakça / References" section.
2. Extracts in-text citations such as `[12]`, `[3-5]`, `[7, 9]` and builds a set of unique cited references.
3. Parses each bibliography entry into reference number, authors, year, title, and DOI (when present).
4. Compares the two sets and reports:
   - missing sources (cited in the text, absent from the bibliography)
   - unused sources (listed in the bibliography, never cited)
   - duplicate bibliography entries
5. Queries Crossref (and OpenAlex) for each DOI or title, and compares the returned title, authors, and year with what the bibliography states, using fuzzy string matching.
6. Returns the results as a structured JSON report.

Out of scope: the scientific content, arguments, and grammar of the document. The system only checks citation structure and bibliographic accuracy.

## Architecture

The system is organized into four independent modules.

| Module | Responsibility |
|---|---|
| Input and preprocessing | Reads the uploaded .docx in memory with python-docx, splits it into paragraphs, and separates the body text from the bibliography block |
| Parsing and extraction | Extracts citations from the body with regular expressions and parses each bibliography line into structured fields |
| Consistency and validation engine | Compares citation and bibliography sets (intersection and difference), sends DOIs to external APIs asynchronously, and scores title similarity with fuzzy matching |
| Output and reporting | Collects inconsistencies, API errors, and match ratios into a single JSON response |

Data flow:

```
.docx upload
   -> body text + bibliography block
   -> citation set + bibliography dictionary, e.g. {1: {"title": "...", "doi": "..."}}
   -> set comparison (missing / unused / duplicate)
   -> asynchronous Crossref / OpenAlex lookups
   -> fuzzy comparison of title, authors, year
   -> JSON report
```

## Design decisions

**Fuzzy matching instead of exact matching.** Titles typed by hand differ from database records in case, punctuation, and spelling. Titles are compared with a similarity ratio (thefuzz, Levenshtein-based). The default threshold is 80 percent and is intended to be a configurable parameter rather than a hard-coded value. Borderline cases are flagged as "manual review recommended" instead of being decided automatically.

**Asynchronous requests.** A long document can contain 200 or more references. DOI lookups are sent concurrently so that total processing time stays low. The design target is 10 to 15 seconds for a document of that size.

**Privacy.** Uploaded documents may be unpublished or copyrighted. Files are processed in memory and are not written to disk or stored after the analysis finishes.

**Error handling.** Network timeouts, invalid DOIs (HTTP 404), and server errors (HTTP 500) are caught and reported as "DOI not found or API did not respond" in the report, instead of stopping the analysis.

**Rate limits.** To handle API rate limiting (HTTP 429), the design includes a cache for previously queried DOIs and retries with exponential backoff. Requests include a contact e-mail address to use Crossref's Polite Pool.

## Known risks

- **Document format.** python-docx can fail if the bibliography heading uses a non-standard style, the bibliography is placed inside a table, or the file was exported from another program with damaged XML.
- **False positives in citation detection.** A pattern that matches square brackets can mistake `x[1]` or technical abbreviations for citations. Rules should check the text around the brackets, with spaCy as a possible fallback.
- **Threshold choice.** A high threshold produces unnecessary warnings. A low threshold lets genuinely different publications pass.

## Technology stack

- Python 3.9 or later
- FastAPI and Uvicorn (REST API)
- python-docx (document parsing)
- re (citation extraction)
- thefuzz (string similarity)
- httpx or aiohttp (asynchronous HTTP requests)
- Crossref REST API and OpenAlex API (external verification)

## Installation

```bash
git clone https://github.com/tuanatasdemir/Academic-Project.git
cd Academic-Project
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

Start the server:

```bash
uvicorn main:app --reload
```

Then open `index.html` in a browser, or send a document directly to the API:

```bash
curl -X POST "http://127.0.0.1:8000/analyze" \
     -F "file=@your_document.docx"
```

Example response (illustrative format, adjust to the actual output):

```json
{
  "missing_in_bibliography": [14],
  "unused_in_text": [9],
  "duplicates": [[3, 17]],
  "doi_checks": [
    {"ref": 5, "doi": "10.xxxx/xxxxx", "status": "verified", "title_similarity": 94},
    {"ref": 8, "doi": "10.xxxx/yyyyy", "status": "mismatch", "title_similarity": 41},
    {"ref": 11, "doi": "10.xxxx/zzzzz", "status": "not_found"}
  ]
}
```

## Project status

Implemented:

- [ ] Replace this list with what currently works (for example: .docx parsing, citation extraction, Crossref lookup)

In progress:

- [ ] Replace with current work (for example: bibliography line parsing, OpenAlex integration)

Planned:

- [ ] Caching of queried DOIs and retry with exponential backoff
- [ ] Configurable similarity threshold exposed as an API parameter
- [ ] Support for author-year citations such as (Author, 2023)
- [ ] Automated tests with sample documents and a measured accuracy figure
- [ ] Web interface improvements

## Author

Nimet Tuana Taşdemir, Computer Engineering, Tekirdağ Namık Kemal University.
