# Zenodo Economics Replication Repository Harvester

This project inventories economics replication records on Zenodo and can download their public files. One shared crawler supports four journal/community profiles:

| Profile | Alias | Zenodo community | Coverage |
| --- | --- | --- | --- |
| `the-economic-journal` | `ej` | [`ej-replication-repository`](https://zenodo.org/communities/ej-replication-repository/records) | The Economic Journal |
| `restud` | `restud` | [`restud-replication`](https://zenodo.org/communities/restud-replication/records) | The Review of Economic Studies |
| `econometric-society` | `es` | [`es-replication-repository`](https://zenodo.org/communities/es-replication-repository/records) | Econometrica, Quantitative Economics, and Theoretical Economics |
| `jeea` | `jeea` | [`jeea_replication`](https://zenodo.org/communities/jeea_replication/records) | Journal of the European Economic Association |

The normalized JSON remains deliberately close to the World Bank Reproducibility Repository scraper format. The journal profiles are data-only JSON files; discovery, normalization, downloads, safety checks, caching, and resume behavior all use the same Python engine.

See [`VALIDATION.md`](VALIDATION.md) for the dated offline and live three-record crawl, bounded-download, checksum, resume, and all-journals test results.

## How it works

1. The Zenodo community records API discovers records.
2. Each record API endpoint supplies complete metadata.
3. Each record files API endpoint supplies the authoritative attachment inventory.
4. Only the exact `entries[].links.content` URL returned for a file is used to download it.

Zenodo does not require a Related Materials HTML scrape. Attachments are first-class API resources, so `files_response.json` replaces the World Bank scraper's `related_materials.html`. The scraper never guesses download URLs from filenames.

Example requests for ReStud:

```text
Community metadata: GET /api/communities/restud-replication
List records:       GET /api/communities/restud-replication/records?page=1&size=25&sort=newest&allversions=false
Fetch metadata:     GET /api/records/21105829
List files:         GET /api/records/21105829/files
Download file:      Use the exact entries[].links.content URL from the files response
```

## Record identifiers

| Identifier | Example | Purpose |
| --- | --- | --- |
| Community slug | `restud-replication` | Selects a journal repository |
| Record ID | `21105829` | Exact published version, API lookup, and local folder |
| Concept record ID | `21105828` | Groups every version of one work |
| Record DOI | `10.5281/zenodo.21105829` | Persistent identifier for the exact version |
| Concept DOI | `10.5281/zenodo.21105828` | Persistent identifier resolving to the latest version |
| File ID | UUID supplied by Zenodo | Individual attachment and local attachment folder |

By default the harvester inventories only the latest version of each record family. `--all-versions` includes every published version.

## Requirements

- Python 3.10 or newer
- Bash
- Internet access
- Enough disk space for requested downloads

There are no third-party Python dependencies.

## Quick start

Make the launcher executable and list the configured communities:

```bash
chmod +x run_scraper.sh
./run_scraper.sh --list-journals
```

Run a two-record Economic Journal smoke test. For compatibility with the original single-community scraper, omitting `--journal` still selects EJ and writes to `smoke-output/`:

```bash
./run_scraper.sh --smoke-test
```

Run a two-record smoke test for each community:

```bash
./run_scraper.sh --all-journals --smoke-test
```

Create one complete inventory without downloading files:

```bash
./run_scraper.sh --journal restud --inventory-only
./run_scraper.sh --journal es --inventory-only
./run_scraper.sh --journal jeea --inventory-only
```

Create inventories for all four configured communities:

```bash
./run_scraper.sh --all-journals --inventory-only
```

Download every public file in one community:

```bash
./run_scraper.sh --journal restud --download-files
```

Resume partial downloads from matching `.part` files:

```bash
./run_scraper.sh --journal restud --download-files --resume
```

`--resume` requires a readable checkpoint with the same community, query, sort, page size, base URL, and version scope.

Several repository packages are multiple gigabytes. A bounded first run is recommended:

```bash
./run_scraper.sh --journal es --download-files --max-file-mb 500 --min-free-gb 10
```

Test only three records with a three-second minimum request interval:

```bash
./run_scraper.sh --journal jeea --inventory-only --max-records 3 --delay 3
```

Refresh cached community, search, record, and file-list responses:

```bash
./run_scraper.sh --journal restud --inventory-only --refresh
```

Inventory every published version:

```bash
./run_scraper.sh --journal jeea --inventory-only --all-versions --output jeea-all-versions
```

Search inside a community:

```bash
./run_scraper.sh --journal es --inventory-only --query 'metadata.title:"replication package"'
```

Advanced users can crawl any other public Zenodo community without adding a profile:

```bash
./run_scraper.sh --community another-public-community --inventory-only --output custom-output
```

For backward compatibility, a bare `--community` run without `--output` uses the legacy `output/` root. Supplying an explicit output is recommended for custom communities; the scope guard will reject reuse of an incompatible existing checkpoint.

View every option:

```bash
./run_scraper.sh --help
```

Run the offline tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Modular layout

```text
src/zenodo_community_harvester/
├── cli.py                    # Argument parsing and profile selection
├── engine.py                 # Shared crawl/checkpoint/download orchestration
├── http.py                   # Requests, retries, rate delay, and streaming
├── model.py                  # Zenodo-to-normalized-JSON conversion
├── platform_catalog.py       # Combined catalog for --all-journals
├── profiles.py               # Typed profile registry and validation
├── util.py                   # Atomic files, paths, checksums, and text helpers
└── community_profiles/       # Data-only journal definitions
    ├── economic-journal.json
    ├── restud.json
    ├── econometric-society.json
    └── jeea.json
```

No journal has a separate downloader or normalizer. See [`docs/ADDING_A_COMMUNITY.md`](docs/ADDING_A_COMMUNITY.md) to add another built-in profile.

## What it collects

For each record, the harvester:

1. Saves the original community search response page.
2. Fetches and saves the exact record API response bytes.
3. Fetches and saves the exact files API response bytes.
4. Creates one normalized entry in the community `catalog.json` and an identical per-record `record.json`.
5. Records the collection profile, community metadata, version and concept IDs, DOIs, creators, access state, licenses, related identifiers, paper links, external links, file UUIDs, sizes, checksums, and exact content URLs.
6. Optionally streams public files to disk without opening or extracting them.

`paper_url` is populated only from a publication-like `metadata.related_identifiers` entry. It remains blank when Zenodo does not supply one. The harvester does not search other websites to fill missing links.

Downloads first go to `.part` files. A completed file is promoted atomically only after its declared size and supported checksum match. Zenodo commonly supplies MD5 checksums; they are used for transfer integrity, not as a cryptographic security guarantee.

## Output

A named single-journal run uses an isolated directory:

```text
output/restud/
├── catalog.json
├── data/
│   ├── raw/
│   │   ├── community.json
│   │   └── search/
│   │       └── page-000001.json
│   └── 21105829/
│       ├── record.json
│       ├── api_response.json
│       ├── files_response.json
│       └── files/
│           └── <file-uuid>/
│               └── replication-package.zip
├── state/
│   └── checkpoint.json
└── logs/
    └── errors.jsonl
```

An all-journals run defaults to `platform-output/` and creates four isolated community trees plus a combined, path-rebased root catalog:

```text
platform-output/
├── catalog.json
├── the-economic-journal/
│   └── catalog.json
├── restud/
│   └── catalog.json
├── econometric-society/
│   └── catalog.json
└── jeea/
    └── catalog.json
```

The root `catalog.json` contains a combined `records` array and `source.community_catalogs`. Local paths in its records are prefixed with the profile key. Each community's `record.json` remains byte-for-JSON identical to its record in that community's own `catalog.json`.

Important files:

- `catalog.json`: Source/run metadata, a summary object, and normalized records.
- `record.json`: Normalized entry for one exact record version.
- `api_response.json`: Original exact-version record response.
- `files_response.json`: Original record-files response.
- `data/raw/search/page-*.json`: Original discovery pages.
- `checkpoint.json`: Crawl scope, discovery progress, record status, and download state.
- `errors.jsonl`: One JSON object per failed stage.

Each attachment is stored under its Zenodo file UUID. If an older response omits the UUID, a deterministic hash-based local file ID is used while the exact original key remains in JSON.

## Normalized JSON

Every community catalog follows this shape:

```json
{
  "schema_version": "2.0.0",
  "generated_at": "2026-08-21T12:00:00Z",
  "source": {
    "repository": "Zenodo",
    "collection": {},
    "community": {},
    "community_api_url": "https://zenodo.org/api/communities/restud-replication",
    "community_records_api_url": "https://zenodo.org/api/communities/restud-replication/records",
    "version_scope": "latest_only"
  },
  "run": {},
  "summary": {},
  "records": []
}
```

Each normalized record includes:

```text
schema_version, source_repository
collection, community
identifiers { record_id, concept_record_id, doi, concept_doi }
title, description_html, description_text
publication_date, created, updated
version, version_index, is_latest_version
resource_type, access, licenses
creators, contributors, keywords, subjects
language, publisher, journal
paper_url, paper_links, related_identifiers, external_links, references
hosted_files[]
links, statistics
methods[]
local_paths
source_status, files_source_status
```

The summary includes record, paper-link, external-link, attachment, access, fallback, estimated-byte, downloaded-byte, unknown-size, skipped-file, and error counts. The root all-journals catalog also includes community success/failure counts.

## Optional authentication

Public records and files require no token. A Zenodo personal access token permits a larger search page size:

```bash
export ZENODO_TOKEN='your-token'
./run_scraper.sh --journal restud --inventory-only --page-size 100
```

The token is sent only in the `Authorization` header. It is never placed in URLs, checkpoints, catalogs, or error logs.

You can identify your crawler with a project/contact user agent:

```bash
export ZENODO_USER_AGENT='my-research-harvester/2.0 (mailto:researcher@example.edu)'
```

## Caching and resume behavior

- Each output directory is bound to one community, base URL, query, sort, page size, and version scope. Reusing it for another scope is rejected.
- `--all-journals` keeps checkpoints and numeric record folders in separate profile directories.
- Before an all-journals run starts, every child output/checkpoint scope is preflighted so a later mismatch cannot leave earlier communities partially updated.
- Valid cached raw JSON is reused unless `--refresh` is supplied.
- Corrupt cached JSON is refetched automatically.
- A final file is trusted only when its current size and, when available, checksum match current API metadata.
- A `.part` file is appended only after a matching `206 Partial Content` response. If the server ignores the range and returns `200`, the partial file is safely restarted.
- Pagination results are deduplicated by exact-version Record ID and checked for cycles and stalls.

Zenodo pagination is not a transactional snapshot. Records added or removed during a long crawl can shift later pages, so generation timestamps, raw pages, deduplication, and refresh support are retained for auditability.

## Safety

- Only public files with exact Zenodo content links are downloaded.
- External links and related identifiers are recorded but never followed.
- Authentication, embargoes, hidden files, and access restrictions are not bypassed.
- Archives are not opened, extracted, imported, or executed.
- File names and IDs are sanitized, and resolved paths must remain inside the selected output.
- Downloads require HTTPS and an official Zenodo host; unsafe redirects and lookalike hosts are rejected.
- API origins are pinned to `https://zenodo.org` or `https://sandbox.zenodo.org`; bearer tokens are never sent to arbitrary or loopback origins.
- Symlink download targets are rejected.
- `--max-file-mb` is enforced from metadata, response headers, and streamed bytes.
- `--min-free-gb` reserves disk space before and during downloads.
- JSON, checkpoints, and completed files use atomic replacement.
- The default request interval is 2.1 seconds.
- Check each record's license before reusing or redistributing its files.

Generated output directories are ignored by Git because they may be large and contain record-specific licenses.

## API references

- [Zenodo REST API and rate limits](https://developers.zenodo.org/)
- [InvenioRDM community records API](https://inveniordm.docs.cern.ch/reference/rest_api_communities/)
- [InvenioRDM records and files API](https://inveniordm.docs.cern.ch/reference/rest_api_drafts_records/)
- [Zenodo version management](https://help.zenodo.org/docs/deposit/manage-versions/)
