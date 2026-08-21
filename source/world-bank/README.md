# World Bank Reproducibility Repository Scraper

This project inventories records from the [World Bank Reproducible Research Repository](https://reproducibility.worldbank.org/catalog/) and can download their public files.


The scraper uses:

1. The public JSON API to find records and collect metadata.
2. Each record's **Related Materials** page to find attachments.
3. The exact file links shown on that page to download files.

The API handles metadata and pagination. The HTML page provides the complete attachment list.

## Record Identifiers

The repository uses three different identifiers:

| Identifier   | Example           | Purpose                           |
| ------------ | ----------------- | --------------------------------- |
| Reference ID | `RR_WLD_2025_394` | API lookup and local folder name  |
| Catalog ID   | `305`             | Record and Related Materials page |
| Resource ID  | `918`             | Individual attachment             |

Example requests:

```text
List records:      GET /api/catalog?page=1&ps=100
Fetch metadata:    GET /api/catalog/RR_WLD_2025_394
Find attachments:  GET /catalog/305/related-materials
Download file:     Use the exact attachment link from the page
```

The scraper never guesses download URLs from filenames.

## Requirements

* Python 3.10 or newer
* Bash
* Internet access
* Enough disk space for downloaded files


## Quick Start

Make the launcher executable:

```bash
chmod +x run_scraper.sh

./run_scraper.sh --smoke-test
```

The smoke test writes to `smoke-output/` and does not overwrite a full crawl.

Create an inventory without downloading files:

```bash
./run_scraper.sh --inventory-only
```

Download all public repository files:

```bash
./run_scraper.sh --download-files
```

Resume an interrupted download:

```bash
./run_scraper.sh --download-files --resume
```

Skip files larger than 500 MiB:

```bash
./run_scraper.sh --download-files --max-file-mb 500
```

Keep at least 10 GiB of disk space free:

```bash
./run_scraper.sh --download-files --min-free-gb 10
```

Test only 10 records with a two-second delay:

```bash
./run_scraper.sh --inventory-only --max-records 10 --delay 2
```

Refresh cached metadata and attachment pages:

```bash
./run_scraper.sh --inventory-only --refresh
```

View all options:

```bash
./run_scraper.sh --help
```

Run the offline tests:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

## What It Collects

For each record, the scraper:

1. Fetches its API metadata.
2. Saves the original API response.
3. Creates a normalized entry in `catalog.json` and `record.json`.
4. Fetches its Related Materials page.
5. Records hosted files and external links.
6. Optionally downloads repository-hosted files.

Missing paper links remain blank. The scraper does not search other websites to fill them in.

Downloads are first written as `.part` files and renamed after completion.

## Output

```text
worldbank-repro-scraper/
├── catalog.json
├── data/
│   └── RR_WLD_2025_394/
│       ├── record.json
│       ├── api_response.json
│       ├── related_materials.html
│       └── files/
│           ├── 916/
│           │   └── README.pdf
│           └── 918/
│               └── RR_WLD_2025_394.zip
├── state/
│   └── checkpoint.json
└── logs/
    └── errors.jsonl
```

Important files:

* `catalog.json`: One document containing a `summary` object (record counts,
  paper-link counts, attachment counts, and estimated download size) and a
  `records` array with every normalized record.
* `record.json`: The normalized entry for one record, including its hosted files
  and external links. Identical to that record's element in the `records` array.
* `api_response.json`: Original API metadata.
* `related_materials.html`: Saved Related Materials page.
* `checkpoint.json`: Progress for interrupted runs.
* `errors.jsonl`: One JSON line per failed metadata, page, or download step.

Each attachment is stored in its resource ID folder to prevent filename conflicts.

## Safety

* Only public files hosted by the repository are downloaded.
* External links are recorded but not followed.
* Authentication and access restrictions are not bypassed.
* Downloaded archives are not opened, extracted, or executed.
* `--max-file-mb` limits individual file sizes.
* `--min-free-gb` preserves free disk space.
* Use `--delay` to avoid sending requests too quickly.
* Check each record's license before reusing or redistributing its files.

Generated data folders are ignored by Git because they may be large or contain files with record-specific licenses.
