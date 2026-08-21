# AJPS Verification Materials Scraper

This project inventories published verification datasets from the
[American Journal of Political Science (AJPS) Dataverse](https://dataverse.harvard.edu/dataverse/ajps)
and can download their public files.

The [AJPS Verification Policy](https://ajps.org/ajps-verification-policy/) is a
policy page, not a record catalog. It requires authors to place every necessary
verification file in the AJPS collection on Harvard Dataverse. For that reason,
this scraper uses Dataverse's documented APIs instead of scraping the AJPS or
Dataverse HTML pages.

The scraper uses:

1. The public **Dataverse Search API** to discover every published dataset in
   the `ajps` collection subtree.
2. The **Dataverse Native API** to fetch the exact published version reported
   during discovery, including metadata and its complete file manifest.
3. The **Dataverse Data Access API** to download unrestricted files by numeric
   file ID.

External links are recorded but never followed. The scraper never guesses a
download URL from a filename or from an internal storage identifier.

## Record Identifiers

The repository exposes four useful identifiers. They are not interchangeable.

| Identifier | Example | Purpose |
| --- | --- | --- |
| Collection alias | `ajps` | Limits discovery to the AJPS subtree |
| Dataset persistent ID | `doi:10.7910/DVN/0XQNDR` | Canonical record ID and version lookup |
| Dataset database ID | `123456` | Harvard Dataverse's installation-local dataset ID |
| File ID | `2424099` | Exact Data Access API download endpoint and local file folder |

Example requests:

```text
Discover datasets:
GET /api/search?q=*&type=dataset&subtree=ajps&fq=publicationStatus:Published

Fetch pinned metadata and files:
GET /api/datasets/:persistentId/versions/2.1
    ?persistentId=doi:10.7910/DVN/0XQNDR&excludeFiles=false

Download an ordinary file:
GET /api/access/datafile/2424099

Download the deposited original of an ingested table:
GET /api/access/datafile/2424099?format=original
```

The dataset DOI is the stable record identity. Numeric database IDs are
preserved as secondary identifiers because they are specific to this Dataverse
installation.

## Requirements

- Python 3.10 or newer
- Bash
- Internet access
- Enough disk space for downloaded files

The scraper uses only the Python standard library. `requirements.txt` is kept
so the repository has the same conventional layout as the other harvesters.

## Quick Start

Make the launcher executable and run the smoke test:

```bash
chmod +x run_scraper.sh
./run_scraper.sh --smoke-test
```

The smoke test inventories at most three records in `smoke-output/`. It does not
overwrite or download into the normal `output/` crawl.

Create a full metadata and file inventory without downloading files:

```bash
./run_scraper.sh --inventory-only
```

Attempt every unrestricted public file that does not require an interactive
guestbook response:

```bash
./run_scraper.sh --download-files
```

Resume an interrupted crawl and continue `.part` downloads:

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

Test only ten records with a two-second delay between requests:

```bash
./run_scraper.sh --inventory-only --max-records 10 --delay 2
```

Refresh cached discovery and dataset metadata:

```bash
./run_scraper.sh --inventory-only --refresh
```

Download Dataverse's archival TSV representation instead of the author's
deposited original for ingested tabular files:

```bash
./run_scraper.sh --download-files --download-format archival
```

Write to a custom directory:

```bash
./run_scraper.sh --inventory-only --output-dir my-ajps-inventory
```

View every option:

```bash
./run_scraper.sh --help
```

Run the offline test suite:

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

## What It Collects

For each published dataset, the scraper:

1. Pins the exact `major.minor` version returned by discovery.
2. Saves the unmodified Search API page that discovered it.
3. Saves the unmodified Native API dataset-version response.
4. Creates the same normalized record in `catalog.json` and `record.json`.
5. Records authors, descriptions, subjects, keywords, license, related
   publications, hosted files, and explicit external links.
6. Preserves both the archival and deposited-original representations of
   ingested tabular files.
7. Optionally attempts unrestricted files through the numeric file ID endpoint.
   A guestbook-required file is left as `failed` and recorded in `errors.jsonl`;
   the scraper never submits personal details or fabricates a guestbook response.

The scraper does not search Wiley, author websites, DOI services, or other
repositories to fill missing paper links. Missing values remain `null` or empty
arrays.

## Output

```text
ajps-verification-scraper/
├── README.md
├── requirements.txt
├── run_scraper.sh
├── scraper.py
├── tests/
│   └── test_scraper.py
└── output/
    ├── catalog.json
    ├── data/
    │   ├── raw/
    │   │   └── search/
    │   │       └── page_000001.json
    │   └── doi_10.7910_DVN_0XQNDR--b3effbe2/
    │       ├── record.json
    │       ├── api_response.json
    │       └── files/
    │           └── 2424099/
    │               └── replication_files.zip
    ├── state/
    │   ├── discovered.json
    │   └── checkpoint.json
    └── logs/
        └── errors.jsonl
```

The short hash at the end of a record directory prevents two distinct
persistent IDs from collapsing to the same path-safe name.

Important files:

- `catalog.json`: one document with `source`, `summary`, and `records`.
- `record.json`: one normalized record, identical to its corresponding element
  in `catalog.json.records`.
- `api_response.json`: the original Native API response for the pinned dataset
  version.
- `data/raw/search/page_*.json`: original paginated discovery responses.
- `discovered.json`: the DOI, pinned version, and original search item for each
  discovered dataset.
- `checkpoint.json`: atomic progress state used by `--resume`.
- `errors.jsonl`: one JSON object per failed metadata or download step.

Every downloaded attachment is stored in its numeric file-ID folder, so two
files with the same label cannot overwrite each other. Dataverse directory
labels are recorded as metadata but are not trusted as local paths.

## Normalized JSON

The top-level catalog has this shape:

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-08-21T10:00:00Z",
  "source": {
    "name": "American Journal of Political Science verification materials",
    "policy_url": "https://ajps.org/ajps-verification-policy/",
    "collection_alias": "ajps",
    "collection_url": "https://dataverse.harvard.edu/dataverse/ajps",
    "api_base_url": "https://dataverse.harvard.edu",
    "version_policy": "published version pinned at discovery time"
  },
  "summary": {
    "source_total_records": 0,
    "records_discovered": 0,
    "records_in_catalog": 0,
    "records_complete": 0,
    "records_failed": 0,
    "records_with_paper_links": 0,
    "external_link_count": 0,
    "hosted_file_count": 0,
    "public_file_count": 0,
    "restricted_file_count": 0,
    "embargoed_file_count": 0,
    "estimated_public_download_bytes": 0,
    "download_status_counts": {},
    "errors_this_run": 0
  },
  "records": []
}
```

A normalized record looks like this:

```json
{
  "schema_version": "1.0",
  "record_id": "doi:10.7910/DVN/0XQNDR",
  "persistent_id": "doi:10.7910/DVN/0XQNDR",
  "dataset_id": 123456,
  "storage_key": "doi_10.7910_DVN_0XQNDR--b3effbe2",
  "title": "Replication Data for: Example Article",
  "authors": [
    {
      "name": "Author, Example",
      "affiliation": "Example University",
      "identifier_scheme": "ORCID",
      "identifier": "0000-0000-0000-0000"
    }
  ],
  "paper": {
    "citation": "Author. 2026. Example Article.",
    "identifier_type": "doi",
    "identifier": "10.1111/ajps.00000",
    "url": "https://doi.org/10.1111/ajps.00000"
  },
  "version": {
    "number": "2.1",
    "state": "RELEASED",
    "create_time": "2026-01-01T00:00:00Z",
    "last_update_time": "2026-02-01T00:00:00Z",
    "release_time": "2026-02-01T00:00:00Z"
  },
  "hosted_files": [],
  "external_links": [],
  "methods": [],
  "harvest_status": "complete"
}
```

Each hosted-file object separates Dataverse's archival representation from the
author's deposited original:

```json
{
  "file_id": 2424099,
  "persistent_id": "doi:10.7910/DVN/ABC123/FILE01",
  "filename": "analysis.dta",
  "directory_label": "data",
  "size_bytes": 889043,
  "checksum": {
    "type": "MD5",
    "value": "7d6821e2c418eab9b3cb3f1f090eb7f6",
    "scope": "original_or_stored_file"
  },
  "archival_representation": {
    "filename": "analysis.tab",
    "content_type": "text/tab-separated-values",
    "size_bytes": 930314
  },
  "original_representation": {
    "filename": "analysis.dta",
    "content_type": "application/x-stata",
    "size_bytes": 889043
  },
  "access": {
    "restricted": false,
    "embargo": null,
    "embargo_active": false,
    "status": "public_metadata",
    "downloadable_without_auth": null
  },
  "urls": {
    "landing_page": "https://dataverse.harvard.edu/file.xhtml?persistentId=doi%3A10.7910%2FDVN%2FABC123%2FFILE01",
    "download": "https://dataverse.harvard.edu/api/access/datafile/2424099?format=original"
  },
  "download": {
    "requested_format": "original",
    "status": "not_requested",
    "local_path": "data/doi_10.7910_DVN_ABC123--hash/files/2424099/analysis.dta",
    "bytes_written": null,
    "checksum_verified": null,
    "attempted_at": null,
    "error": null
  }
}
```

## Resume Behavior

Downloads are written to `filename.part` and atomically renamed only after the
transfer and available size/checksum checks succeed.

With `--resume`:

- completed metadata records are loaded from `record.json`;
- valid final files are not downloaded again;
- a partial file is continued with `Range: bytes=N-` when the server returns
  `206 Partial Content` and a matching `Content-Range`;
- if the server ignores the range and returns `200`, the `.part` file is safely
  restarted from byte zero;
- a final file is never exposed under its final name until validation succeeds.

## Safety

- Only documented public Dataverse APIs are called.
- The scraper does not read or send API tokens; it is public-only.
- External metadata links are recorded but never followed.
- Authentication, guestbooks, access requests, and restrictions are not bypassed.
- Internal `storageIdentifier` values and signed redirect URLs are never used to
  construct download requests.
- Downloaded archives are not opened, extracted, or executed.
- Record and filename components are sanitized; directory labels never become
  local paths.
- `--max-file-mb` limits both the advertised and actual streamed file size.
- `--min-free-gb` preserves a free-space reserve before and during downloads.
- Requests are sequential, delayed by default, retried with backoff, and honor
  `Retry-After` for temporary failures.
- Raw metadata is cached, progress is checkpointed atomically, and errors are
  written as JSON Lines.
- Check each dataset's license and terms before reusing or redistributing files.

Generated output directories are ignored by Git because they may be large or
contain files with dataset-specific licenses.

## API Documentation

- [Dataverse Search API](https://guides.dataverse.org/en/latest/api/search.html)
- [Dataverse Native API](https://guides.dataverse.org/en/latest/api/native-api.html)
- [Dataverse Data Access API](https://guides.dataverse.org/en/latest/api/dataaccess.html)
- [Harvard Dataverse API Terms of Use](https://support.dataverse.harvard.edu/harvard-dataverse-api-terms-use)
