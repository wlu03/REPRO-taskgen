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

### Downloading from openICPSR

Every page on `www.openicpsr.org` — the project landing page as well as the
package endpoint — is served behind a Cloudflare **managed challenge**. Plain
HTTP clients receive `403` with `cf-mitigated: challenge` no matter what
headers they send, because clearance is granted only after a browser executes
the challenge and is then bound to the client's address, User-Agent, and TLS
fingerprint. Metadata is unaffected: `search.icpsr.umich.edu` and
`bibliography.icpsr.umich.edu` are ordinary hosts, which is why inventory runs
succeed while downloads do not.

### Credentials (required before any download)

Downloads need a signed-in ICPSR account. Create `.env` in this directory — it
is gitignored, and `run_scraper.sh` loads it automatically:

```bash
cp .env.example .env
$EDITOR .env          # fill in ICPSR_EMAIL and ICPSR_PASSWORD
```

```ini
ICPSR_EMAIL=you@example.com
ICPSR_PASSWORD=your-password
```

Never put these values in a source file. `browser_login.py` and `scraper.py`
read them only from the environment, so a committed file cannot leak them.
Register at <https://www.openicpsr.org/> if you do not have an account yet.

**openICPSR's session cookie (`JSESSIONID`) is a browser-session cookie**: it is
discarded when Chromium exits. A sign-in performed in a separate run therefore
cannot carry over, and the sign-in has to happen in the same run as the
download — which is what `--browser-auto-login` does:

```bash
./run_scraper.sh --download-files --browser --browser-auto-login \
  --max-records 1 --max-file-mb 500
```

`browser_login.py` remains useful for checking the credentials interactively,
but its session will not survive for a later scraper run.

Downloads therefore need a person. `--browser` opens a persistent Chromium
profile, waits while you solve the challenge, sign in, and accept the study's
terms, and then transfers the package through that same browser:

```bash
./.venv/bin/python -m pip install -r requirements-browser.txt
./.venv/bin/python -m playwright install chromium
./run_scraper.sh --download-files --browser --max-records 1 --max-file-mb 500
```

The profile directory (`browser-profile/` by default, or `--browser-profile`)
is reused between runs so an already-cleared session is not thrown away.
`--browser-wait` bounds how long each study waits for you (default 600s); when
it expires the study is recorded as `access_blocked` and the run moves on.
`--browser-headless` is only useful once a profile is already cleared — a
headless browser cannot solve a challenge on its own.

Nothing in this repository attempts to defeat, forge, or replay a challenge.
Clearance comes from a person, and each study's terms are accepted by that
person, not by the scraper.

Plain `--cookie-file` remains available for hosts that do not challenge, but
it will not clear Cloudflare on its own:

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
