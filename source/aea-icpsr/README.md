# ICPSR AEA Scraper

This repository inventories studies in the American Economic Association Data
and Code Repository at ICPSR and can optionally attempt to download each
study's current project ZIP.

The scraper uses ICPSR's public search service with the `aea` archive filter.
It does **not** crawl the robots-disallowed `/sites/*/search/*` pages. ICPSR's
documented Object Export API is not used because its current scope excludes
self-deposited AEA/openICPSR records.

## Requirements

- Python 3.10 or newer
- Bash
- Internet access

## Run

The shell wrapper creates a virtual environment and installs the pinned Python
dependency:

```bash
chmod +x run_scraper.sh
./run_scraper.sh --inventory-only
```

Useful bounded runs:

```bash
./run_scraper.sh --inventory-only --max-records 10
./run_scraper.sh --inventory-only --study-id 120506
```

Query ICPSR's bibliography JSON endpoint and keep an explicit paper URL when
one is exposed:

```bash
./run_scraper.sh --inventory-only --fetch-paper-links
```

The raw response is saved as `related_publications.json`. Dataset citations and
the package's own `10.3886/...` DOI are excluded when choosing the paper link.
If access fails, the scraper records a fetch error and leaves the paper URL
blank unless a previously verified value exists. Links in a study summary are
kept as resources but are never promoted to paper links.

Attempt project ZIP downloads:

```bash
./run_scraper.sh --download-files --max-file-mb 500
```

ICPSR may require a logged-in session and terms acceptance even for public
packages. If you already have an authorized session, the scraper can load a
local Netscape-format cookie jar:

```bash
./run_scraper.sh --download-files --cookie-file /path/to/cookies.txt
```

Do not place passwords or cookies in this repository. Login redirects, terms
pages, and access challenges are labeled in JSON instead of being saved as ZIP
files.

Resume a stopped crawl from cached search/detail responses and verify any
completed downloads by SHA-256:

```bash
./run_scraper.sh --download-files --resume
```

Run offline tests:

```bash
./run_scraper.sh --help
./.venv/bin/python -m unittest discover -s tests -v
```

## Output

```text
icpsr-aea-scraper/
├── catalog.json
├── data/
│   └── 120506/
│       ├── search_record.json
│       ├── related_publications.json  # only after a successful optional check
│       ├── record.json
│       └── files/
│           └── 120506-V1.zip          # only after a successful download
├── state/
│   ├── checkpoint.json
│   └── search_pages/
└── logs/
    └── errors.jsonl                   # only when errors occur
```

`catalog.json` is the single combined output:

```json
{
  "summary": {
    "reported_catalog_total": 0,
    "processed_records": 0,
    "paper_links_present": 0,
    "paper_links_missing": 0,
    "packages_found": 0,
    "download_status_counts": {}
  },
  "records": []
}
```

Each item in `records` exactly matches its `data/<study_id>/record.json`. A
missing or unchecked paper link remains:

```json
"paper": {
  "title": "",
  "authors": "",
  "citation": "",
  "url": "",
  "url_source": "",
  "link_status": "not_checked",
  "outputs": []
}
```

Use `--fetch-paper-links` to distinguish a confirmed `absent` link from a
`fetch_error`. The complete citation array is also embedded in that study's
normalized record. The scraper never guesses a paper URL from a study title.

## Access and reuse

- Inventory mode is the default; downloading thousands of packages can require
  substantial storage and a valid ICPSR session.
- An unbounded inventory must discover exactly the total ICPSR reports; the
  run fails instead of silently accepting a partial catalog.
- Archives are streamed to `.part` files, checked as ZIPs, hashed, and renamed
  only after completion. They are never extracted or executed.
- External links found in summaries are recorded but never followed.
- Keep the ICPSR backlink and `metadata_download_date` in downstream catalogs.
  ICPSR describes its metadata as
  [CC BY-NC 4.0](https://www.icpsr.umich.edu/sites/icpsr/about/repository-operations/accessing-metadata).
- Package contents remain subject to each study's license, terms, and citation
  requirements. Review them before use or redistribution.
