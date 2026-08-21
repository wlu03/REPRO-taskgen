# Adding a Zenodo community profile

Profiles contain naming data only. Do not copy or subclass the crawler, downloader, HTTP client, or normalizer.

## 1. Verify the public API

For a community slug such as `example-replications`, verify these unauthenticated endpoints:

```text
GET https://zenodo.org/api/communities/example-replications
GET https://zenodo.org/api/communities/example-replications/records?page=1&size=3&sort=newest&allversions=false
GET https://zenodo.org/api/records/<record-id>
GET https://zenodo.org/api/records/<record-id>/files
```

Confirm that the record files response provides an `entries` list and exact `entries[].links.content` URLs. Do not add URL templates for downloads.

## 2. Add one data file

Create `src/zenodo/community_profiles/<profile>.json`:

```json
{
  "key": "example-journal",
  "slug": "example-replications",
  "title": "Example Journal",
  "abbreviation": "EJX",
  "aliases": ["example", "ejx"],
  "records_url": "https://zenodo.org/communities/example-replications/records"
}
```

Rules:

- `key` is the stable CLI/output name.
- `slug` must exactly preserve Zenodo punctuation, including underscores.
- `aliases` cannot conflict with another profile's key, slug, abbreviation, or aliases.
- `records_url` must be the official HTTPS records page matching `slug`.
- Do not store totals; they change over time.
- Do not store API paths, file URL patterns, headers, or schema hooks.

If presentation order matters, add the key to `_PROFILE_ORDER` in `profiles.py`.

## 3. Add regression coverage

Update the expected profile-to-slug mapping in `tests/test_profiles.py`. The offline all-profiles integration test must prove that the profile:

- reaches its exact community and records endpoints;
- uses the shared engine;
- writes an isolated checkpoint/output tree;
- adds its records to the path-rebased platform catalog;
- keeps each `record.json` identical to its community catalog element.

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 4. Run a bounded live check

Start with metadata only:

```bash
./run_scraper.sh --journal example-journal --inventory-only --max-records 3 --refresh --output live-check-output/example-journal
```

Then request downloads with explicit disk limits:

```bash
./run_scraper.sh --journal example-journal --download-files --resume --max-records 3 --max-file-mb 50 --min-free-gb 1 --output live-check-output/example-journal
```

Validate that:

- the command exits successfully;
- `record.json` equals its community catalog element;
- every downloaded byte count matches `size_bytes`;
- every supported checksum matches;
- oversized files are marked `skipped_safety_limit` before download;
- no `.part` files or current-run errors remain.
