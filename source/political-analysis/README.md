# PAN Dataverse Scraper

This project inventories records from the [Political Analysis Dataverse](https://dataverse.harvard.edu/dataverse/pan) collection and can download the files each dataset exposes.

There are two metadata sources, selected with `--source`.

### `--source api` (default)

1. The Dataverse Search endpoint lists every dataset in the collection.
2. The Croissant metadata export supplies each dataset's metadata and complete file list.
3. Files are downloaded from the exact `contentUrl` the export supplies.

### `--source html`

1. The collection's HTML result pages find dataset pages.
2. The Schema.org or Croissant JSON-LD embedded in each dataset's HTML supplies metadata and file links.
3. Files are downloaded from the exact URLs the page exposes.

## Why the API is the default

The HTML path was the original design. Two things changed on Harvard's side:

* HTML routes are served behind an AWS WAF browser-verification challenge. Any
  non-browser client gets `HTTP 202` and a JavaScript challenge page instead of
  content, so the HTML path needs `--cookie-file` to run at all.
* Dataset pages now embed a Croissant block with **no file list**. Parsing that
  HTML yields metadata but no download URLs.

A live comparison over the same 10 datasets:

| | `--source html` | `--source api` |
| --- | --- | --- |
| Needs browser cookies | yes | no |
| Files found | 44 (plus 42 duplicate preview links) | complete list |
| Files with a download URL | 7 | all |
| Files with a checksum | 0 | all |

Across 419 datasets sampled from the full collection, the export returned 1750
files with a `contentUrl` and a checksum for **every single one**.

## Important Download Detail

Both sources yield file URLs of this shape, supplied by Harvard rather than
built by this program:

```text
https://dataverse.harvard.edu/api/access/datafile/123456
```

The scraper does not construct, guess, or pattern-match those URLs. Under
`--source api` they come from the metadata export's `contentUrl`; under
`--source html` they come from the dataset page.

That endpoint answers with a `303` redirect to presigned object storage on
`dvn-cloud-iqss.s3.amazonaws.com`. Downloads are allowed to finish on that
host and on the repository itself; any other redirect target is refused unless
`--allow-external-downloads` is set, and the final host is recorded in each
file entry as `redirected_to`.

## Rate limits

The API is not rate-limit-free. A sweep at roughly three requests per second
was cut off with `HTTP 403` partway through the collection, and the block
covered the whole API for several minutes. The default `--delay 1.0` is the
conservative setting; do not lower it for a full crawl.

## Requirements

- Python 3.11 or newer
- Bash
- Internet access
- Enough disk space for downloaded files

## Quick Start

Optionally identify yourself to the repository by creating a `.env` file. This
only appends a `contact=` fragment to the User-Agent; the scraper runs without
it.

```dotenv
PAN_CONTACT_EMAIL=you@example.com
```

Make the launcher executable:

```bash
chmod +x run_scraper.sh
```

Run a five-record smoke test:

```bash
./run_scraper.sh --smoke-test
```

The smoke test writes to `smoke-output/` and does not download files.

Force the HTML path instead of the default API path:

```bash
./run_scraper.sh --inventory-only --source html --cookie-file cookies.txt
```

Create the complete inventory without downloading files:

```bash
./run_scraper.sh --inventory-only
```

Download all files exposed by the dataset HTML:

```bash
./run_scraper.sh --download-files
```

Resume interrupted downloads:

```bash
./run_scraper.sh --download-files --resume
```

Skip files larger than 500 MB:

```bash
./run_scraper.sh --download-files --max-file-mb 500
```

Keep at least 10 GiB of free disk space:

```bash
./run_scraper.sh --download-files --min-free-gb 10
```

Test only 10 datasets with a two-second request delay:

```bash
./run_scraper.sh --inventory-only --max-records 10 --delay 2
```

Download only ZIP and PDF files:

```bash
./run_scraper.sh --download-files --include-ext .zip,.pdf
```

Refresh cached HTML pages:

```bash
./run_scraper.sh --inventory-only --refresh
```

Preview selected downloads without transferring file bytes:

```bash
./run_scraper.sh --download-files --dry-run
```

View all options:

```bash
./run_scraper.sh --help
```

Run the offline tests:

```bash
make test
```

## What It Collects

For each dataset under `--source api`, the scraper:

1. Finds it in a saved Search endpoint page.
2. Saves the original Croissant metadata export as `croissant.json`.
3. Saves the same document as `structured_data.json`.
4. Creates a normalized `record.json`.
5. Adds one normalized entry to `catalog.json`.
6. Records file names, MIME types, sizes, checksums, and exact download URLs.
7. Optionally downloads the exposed files.

Under `--source html` steps 1-3 instead save the collection page, the dataset
HTML, and the JSON-LD blocks embedded in it.

Missing paper links remain blank. The scraper does not search other websites to fill them in.

Downloads are written as `.part` files and renamed after completion. Existing complete files are checked before they are downloaded again.

## Output

```text
pan-dataverse-scraper/

├── catalog.json
├── inventory_summary.json
├── missing_paper_links.json
├── data/
│   ├── _search_pages/                     (--source api)
│   │   └── page_0001.json
│   ├── _collection_pages/                 (--source html)
│   │   └── pan/
│   │       ├── page_0001.html
│   │       └── page_0001.html.http.json
│   └── doi_10.7910_DVN_ABC123--<hash>/
│       ├── croissant.json                 (--source api)
│       ├── dataset.html                   (--source html)
│       ├── dataset.html.http.json         (--source html)
│       ├── structured_data.json
│       ├── record.json
│       └── files/
│           └── 123456/
│               └── replication.zip
├── state/
│   └── checkpoint.json
└── logs/
    └── errors.jsonl
```

Important files:

- `catalog.json`: One normalized entry for every successfully parsed dataset.
- `inventory_summary.json`: Dataset, file, extension, byte, and download-status totals.
- `missing_paper_links.json`: Datasets with no explicit paper link in their page metadata.
- `croissant.json`: Original metadata export returned for the dataset.
- `dataset.html`: Original HTML returned for the dataset page (`--source html`).
- `structured_data.json`: The metadata document, or all JSON-LD blocks embedded in the HTML.
- `record.json`: Normalized metadata for one dataset.
- `checkpoint.json`: Progress for interrupted runs.
- `errors.jsonl`: Collection, parsing, access, and download failures.

Each file is stored in its file ID or URL-hash folder to prevent filename conflicts.

## How Discovery Works

Under `--source api` the scraper requests:

```text
GET /api/search?q=*&subtree=pan&type=dataset&per_page=1000&start=0
```

It reads `data.total_count` and pages with `start` until every dataset is seen,
deduplicating on the persistent identifier. Each saved page is kept under
`data/_search_pages/`.

Under `--source html` it requests ordinary collection pages such as:

```text
GET /dataverse/pan?q=&types=dataverses%3Adatasets&sort=dateSort&order=desc&page=1
```

It parses `.datasetResult` cards for dataset links and `.dataverseResult` cards for child collections. Pagination follows the ordinary `page=` links rendered in the HTML.

## How Dataset Metadata Is Parsed

Under `--source api` the scraper requests the Croissant export that the dataset
page advertises in its own `Link: rel="describedby"` header:

```text
GET /api/datasets/export?exporter=croissant&persistentId=doi:10.7910/DVN/ABC123
```

Files come from the `distribution` array's `cr:FileObject` entries, each of
which carries `name`, `encodingFormat`, `contentSize`, `md5`, and `contentUrl`.
A dataset whose export has no file objects is recorded with an empty file list
rather than an error; 21 of 626 datasets were in that state when last checked.

## How Dataset Pages Are Parsed

Dataverse places structured metadata in the HTML `<head>`:

```html
<script type="application/ld+json">
{
  "@type": "Dataset",
  "name": "Example Dataset",
  "distribution": [
    {
      "@type": "DataDownload",
      "name": "replication.zip",
      "contentUrl": "https://dataverse.harvard.edu/api/access/datafile/123456"
    }
  ]
}
</script>
```

The scraper supports both Schema.org `Dataset`/`DataDownload` objects and Croissant `sc:Dataset`/`cr:FileObject` objects.

If JSON-LD is missing, it falls back to Dublin Core/Open Graph metadata and ordinary file links present in the page. The fallback may expose less information than JSON-LD.

## Browser Verification Pages

The scraper does not bypass CAPTCHA or anti-bot controls. It stops when Harvard Dataverse returns a browser-verification page instead of the dataset HTML. This affects `--source html`; the API routes are not currently challenged.

A Netscape-format cookie file from an authorized browser session can be supplied when needed:

```bash
./run_scraper.sh --inventory-only --cookie-file cookies.txt
```

Cookies are used only for HTTP requests and are not written to `catalog.json` or saved response metadata.

## Safety

- Discovery and metadata come from public endpoints the dataset pages advertise, or from the pages themselves under `--source html`.
- Download URLs are used exactly as Harvard supplies them.
- File URLs are never guessed from filenames or numeric identifiers.
- Downloads may finish on the repository or its object storage; any other host is skipped unless `--allow-external-downloads` is set.
- Authentication and access restrictions are not bypassed.
- Guestbook responses are not invented or submitted.
- Downloaded archives are not opened, extracted, imported, or executed.
- `--max-file-mb` limits individual file sizes.
- `--min-free-gb` preserves free disk space.
- `--delay` controls request frequency.
- Check each dataset's license before reusing or redistributing its files.

Generated output directories are ignored by Git because they may be large or contain files with record-specific licenses.
