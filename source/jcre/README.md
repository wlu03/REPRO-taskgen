# JCRE Replication Package Scraper

This project inventories publications from the [Journal of Comments and Replications in Economics](https://jcr-econ.org/publications/) and its predecessor, the **International Journal for Re-Views in Empirical Economics**, then optionally downloads public replication files hosted by the **ZBW Journal Data Archive**.

The scraper uses:

1. The JCRE **Publications** HTML page to discover publications and collect article metadata.
2. Each publication's code/data link to identify its replication package.
3. DOI redirects to reach the package's ZBW Journal Data landing page.
4. The public CKAN API to collect package metadata and the complete resource list.
5. The exact resource URLs returned by CKAN to download repository-hosted files.

The publications page is the discovery source. The CKAN API is the package and resource metadata source. The scraper does not derive download URLs from filenames.

## Record Identifiers

Several identifiers appear in one record. They are not interchangeable.

| Identifier | Example | Purpose |
| --- | --- | --- |
| Local record ID | `JCRE_81781_62` | Publication folder name and checkpoint key |
| Article DOI | `10.18718/81781.62` | Identifies the JCRE/IREE article |
| Replication DOI | `10.15456/j1.2025190.2321978684` | Resolves to the replication package |
| Dataset slug | `replication-package-reexamining-the-effect-of-clean-water` | CKAN `package_show` lookup |
| Dataset ID | CKAN UUID | Stable repository dataset identifier |
| Resource ID | CKAN UUID | Identifies one file or external resource |

The local record ID is derived from the article DOI. For example:

```text
10.18718/81781.62  ->  JCRE_81781_62
```

Example requests:

```text
Discover publications:
GET https://jcr-econ.org/publications/

Resolve a replication DOI:
GET https://doi.org/10.15456/j1.2025190.2321978684

Fetch package metadata:
GET https://journaldata.zbw.eu/api/3/action/package_show?id=<dataset-slug>

Download a file:
Use the exact URL in result.resources[].url
```

The scraper extracts the dataset slug from the resolved Journal Data landing URL. If necessary, it performs a CKAN search and accepts only an exact replication-DOI match. It never guesses a dataset slug from an article title.

## Requirements

- Python 3.10 or newer
- Bash
- Internet access
- Enough disk space for downloaded replication files

## Quick Start

Make the launcher executable:

```bash
chmod +x run_scraper.sh
```

Run a three-record smoke test:

```bash
./run_scraper.sh --smoke-test
```

The smoke test writes to `smoke-output/` and does not overwrite a full crawl.

Create a complete inventory without downloading files:

```bash
./run_scraper.sh --inventory-only
```

Download all eligible repository-hosted replication files:

```bash
./run_scraper.sh --download-files
```

Resume interrupted `.part` downloads:

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

Test only 10 records with a two-second delay between requests:

```bash
./run_scraper.sh --inventory-only --max-records 10 --delay 2
```

Collect only recent JCRE records:

```bash
./run_scraper.sh --inventory-only --journal JCRE --year-min 2024
```

Collect only the predecessor IREE records:

```bash
./run_scraper.sh --inventory-only --journal IREE
```

Refresh the cached publications page and package metadata:

```bash
./run_scraper.sh --inventory-only --refresh
```

Write to a custom output directory:

```bash
./run_scraper.sh --inventory-only --output data/jcre-crawl
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

For each publication, the scraper:

1. Extracts the journal, volume, year, issue, title, author text, article DOI, and article URL.
2. Saves the exact HTML fragment from the Publications page.
3. Records the publication even when no replication package is linked.
4. Records the code/data availability statement shown by JCRE.
5. Resolves the replication DOI or direct Journal Data link.
6. Saves the landing-page redirect metadata and HTML when available.
7. Fetches and saves the original CKAN package response.
8. Normalizes package metadata, licenses, tags, and resources.
9. Separates repository-hosted uploads from external resources.
10. Optionally downloads eligible hosted files.

Article PDFs are not downloaded. Article links are recorded in metadata.

Missing replication links remain blank. The scraper does not search unrelated websites to fill them in.

Downloads are first written as `.part` files and atomically renamed after completion. Completed files receive a SHA-256 checksum in `record.json` and `catalog.json`.

## Output

```text
jcre-output/
├── catalog.json
├── source/
│   ├── publications.html
│   └── publications_response.json
├── data/
│   └── JCRE_81781_62/
│       ├── record.json
│       ├── publication_fragment.html
│       ├── replication_landing_response.json
│       ├── replication_landing.html
│       ├── ckan_response.json
│       ├── ckan_search_response.json          # only when DOI fallback search is used
│       └── files/
│           └── 11111111-1111-1111-1111-111111111111/
│               └── clean-water-replication.zip
├── state/
│   └── checkpoint.json
└── logs/
    ├── run.log
    └── errors.jsonl
```

Important files:

- `catalog.json`: One document containing source metadata, a `summary` object, and a `records` array with every normalized publication.
- `record.json`: The normalized entry for one publication. It is identical to that publication's element in `catalog.json`.
- `publication_fragment.html`: The exact publication block extracted from the JCRE Publications page.
- `replication_landing_response.json`: The resolved URL, redirect chain, content type, and response metadata for the package link.
- `replication_landing.html`: The saved Journal Data package page when the resolved response is HTML.
- `ckan_response.json`: The original CKAN `package_show` response.
- `ckan_search_response.json`: The original CKAN search response when exact-DOI fallback discovery was needed.
- `checkpoint.json`: Per-record inventory and download progress.
- `errors.jsonl`: One JSON object per parse, resolution, API, disk, or download error.
- `run.log`: Human-readable progress for each run.

Each attachment is stored inside its CKAN resource ID folder to prevent filename conflicts.

## Catalog Shape

An abbreviated record looks like this:

```json
{
  "record_id": "JCRE_81781_62",
  "journal_code": "JCRE",
  "volume": 5,
  "year": 2026,
  "issue": "2026-11",
  "title": "Reexamining the Effect of Clean Water. A Replication Study ...",
  "authors_text": "Ivan Kozlov and Carson Jones",
  "article_doi": "10.18718/81781.62",
  "article_url": "https://doi.org/10.18718/81781.62",
  "methods": [
    {
      "stage": "discover_publication",
      "method": "jcre_publications_html"
    },
    {
      "stage": "fetch_replication_metadata",
      "method": "zbw_journal_data_ckan_package_show"
    },
    {
      "stage": "download_resources",
      "method": "ckan_exact_resource_url"
    }
  ],
  "replication": {
    "availability_text": "The R code and the data are available »here.",
    "doi": "10.15456/j1.2025190.2321978684",
    "dataset_slug": "replication-package-reexamining-the-effect-of-clean-water",
    "dataset_id": "<CKAN UUID>",
    "license_id": "<package license when supplied>",
    "inventory_status": "complete",
    "resources": [
      {
        "resource_id": "<CKAN resource UUID>",
        "name": "clean-water-replication.zip",
        "url": "<exact CKAN resource URL>",
        "size_bytes": 123456,
        "hosted_by_repository": true,
        "downloadable": true,
        "download_status": "not_requested"
      }
    ]
  }
}
```

## Summary Fields

`catalog.json -> summary` includes:

- Total publication records
- Counts by JCRE/IREE
- Earliest and latest publication year
- Records with article DOIs
- Records with replication links
- Unique replication packages referenced
- Package inventory status counts
- Total, hosted, downloadable, and external resources
- Known and unknown resource-size counts
- Known and unknown size counts for download-eligible resources
- Estimated download bytes from eligible resources whose size is reported by CKAN
- Download status counts
- Errors encountered in the current run
- Whether downloads stopped to preserve the free-space reserve

The estimated byte total excludes external resources and eligible resources whose CKAN metadata does not include a size.

## Resume and Cache Behavior

- The Publications page is cached at `source/publications.html`.
- Completed package inventories are reused unless `--refresh` is supplied.
- `--resume` continues an existing `.part` file when the server supports HTTP range requests.
- Existing completed files are not overwritten.
- The checkpoint is updated after each record and after each resource attempt.
- A low-disk-space stop preserves valid `.part` files for a later resumed run.

## Download Eligibility

A CKAN resource is selected for automatic download only when all of the following are true:

1. Its URL uses HTTP or HTTPS.
2. Its URL is hosted by `journaldata.zbw.eu` or `www.journaldata.zbw.eu`.
3. CKAN marks it as an upload, or its URL is a Journal Data download endpoint.
4. It passes the configured file-size limit.
5. Downloading it would preserve the configured free-space reserve.

An additional host can be allowed only as a redirect target for a documented Journal Data storage service:

```bash
./run_scraper.sh --download-files --allow-download-host storage.example.org
```

This option does not cause external CKAN resources to become eligible. It only permits an already eligible Journal Data upload URL to redirect to the named storage host.

## Safety

- Only public replication resources exposed by the ZBW Journal Data CKAN metadata are eligible for download.
- Exact CKAN resource URLs are used; filenames are never converted into guessed URLs.
- External resource links are recorded but not followed or downloaded.
- DOI redirects are restricted to DOI and ZBW Journal Data hosts.
- Authentication and access restrictions are not bypassed.
- Downloaded archives are not opened, extracted, imported, or executed.
- Article PDFs are not downloaded.
- `--max-file-mb` limits individual files.
- `--min-free-gb` preserves a disk-space reserve.
- Requests are sequential and obey `--delay` and `Retry-After` responses.
- Every downloaded file receives a SHA-256 checksum.
- Review each package's license before reuse or redistribution.

Generated output folders are ignored by Git because they may be large and may contain files governed by record-specific licenses.

## Limitations

- The JCRE Publications page is hand-maintained HTML. The parser uses volume headings, article DOI patterns, and the displayed citation structure rather than brittle CSS positions.
- CKAN metadata fields such as file size, MIME type, and license may be absent for some records.
- A publication can have no replication link, and a reply can refer to the same package as another publication.
- Because storage is organized by publication record, a package referenced by multiple publications can be downloaded more than once.
- Non-ZBW package repositories are recorded as external and are not crawled.
- The scraper inventories and downloads packages; it does not execute replication code or verify scientific reproducibility.

## Repository Structure

```text
jcre-replication-scraper/
├── README.md
├── pyproject.toml
├── requirements.txt
├── run_scraper.sh
├── src/
│   └── jcre_scraper/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── ckan.py
│       ├── http.py
│       ├── models.py
│       ├── parser.py
│       ├── scraper.py
│       ├── storage.py
│       └── utils.py
└── tests/
    ├── fixtures/
    │   ├── package_show.json
    │   └── publications.html
    ├── test_ckan.py
    ├── test_http.py
    ├── test_parser.py
    ├── test_scraper.py
    ├── test_storage.py
    └── test_utils.py
```
