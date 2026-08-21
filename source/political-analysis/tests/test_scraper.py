from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_collection_page_extracts_datasets_subcollections_and_pages(self) -> None:
        body = (FIXTURES / "collection_page_1.html").read_bytes()
        parsed = scraper.parse_collection_html(
            body,
            "https://dataverse.harvard.edu/dataverse/pan?page=1",
            "pan",
        )
        self.assertEqual(2, len(parsed.datasets))
        self.assertEqual("doi:10.7910/DVN/AAAAAA", parsed.datasets[0]["persistent_id"])
        self.assertEqual("Dataset Alpha", parsed.datasets[0]["title"])
        self.assertEqual(("pan-sub",), parsed.subcollections)
        self.assertEqual(2, parsed.max_page)
        self.assertEqual(4, parsed.reported_total)

    def test_schemaorg_dataset_metadata_and_files(self) -> None:
        body = (FIXTURES / "dataset_schemaorg.html").read_bytes()
        record, documents = scraper.parse_dataset_html(
            body,
            "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi%3A10.7910%2FDVN%2FAAAAAA",
        )
        self.assertTrue(documents)
        self.assertEqual("doi:10.7910/DVN/AAAAAA", record["persistent_id"])
        self.assertEqual("10.7910/DVN/AAAAAA", record["doi"])
        self.assertEqual("Dataset Alpha", record["title"])
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], record["authors"])
        self.assertEqual("https://example.org/paper", record["paper_url"])
        self.assertEqual(2, len(record["files"]))
        self.assertEqual("111", record["files"][0]["file_id"])
        self.assertEqual(1234, record["files"][0]["size_bytes"])
        self.assertEqual(2048, record["files"][1]["size_bytes"])
        self.assertEqual("embedded_json_ld", record["files"][0]["source"])

    def test_croissant_dataset_is_supported(self) -> None:
        body = (FIXTURES / "dataset_croissant.html").read_bytes()
        record, _ = scraper.parse_dataset_html(
            body,
            "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi%3A10.7910%2FDVN%2FBBBBBB",
        )
        self.assertEqual("Dataset Beta", record["title"])
        self.assertEqual(["Jane Doe"], record["authors"])
        self.assertEqual(1, len(record["files"]))
        self.assertEqual("data/file.csv", record["files"][0]["name"])
        self.assertEqual(42, record["files"][0]["size_bytes"])
        self.assertEqual("0123456789abcdef0123456789abcdef", record["files"][0]["checksums"]["md5"])

    def test_challenge_is_detected(self) -> None:
        body = (FIXTURES / "challenge.html").read_bytes()
        self.assertTrue(scraper.is_bot_challenge(body))
        with self.assertRaises(scraper.BotChallengeError):
            scraper.parse_collection_html(body, "https://example.test/dataverse/pan", "pan")

    def test_collection_url_is_html_not_metadata_api(self) -> None:
        url = scraper.build_collection_url("https://dataverse.harvard.edu", "pan", 3)
        self.assertIn("/dataverse/pan?", url)
        self.assertIn("page=3", url)
        self.assertNotIn("/api/v1/", url)

    def test_safe_segments_remove_path_traversal(self) -> None:
        self.assertEqual("_secret.zip", scraper.safe_segment("../secret.zip"))
        self.assertNotIn("/", scraper.safe_segment("folder/name.zip"))

    def test_breadcrumb_dataverse_links_are_not_followed_as_children(self) -> None:
        body = b"""<!doctype html><html><body>
        <nav><a href='/dataverse/root'>Root Dataverse</a></nav>
        <div class='datasetResult'>
          <a href='/dataset.xhtml?persistentId=doi%3A10.1234%2FTEST'>Dataset</a>
        </div>
        </body></html>"""
        parsed = scraper.parse_collection_html(
            body,
            "https://dataverse.harvard.edu/dataverse/pan?page=1",
            "pan",
        )
        self.assertEqual((), parsed.subcollections)


class IntegrationTests(unittest.TestCase):
    def test_complete_local_html_inventory_and_download(self) -> None:
        payload = b"PK\x03\x04local-test-package"
        digest = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                split = urlsplit(self.path)
                if split.path == "/dataverse/pan":
                    query = parse_qs(split.query)
                    self.assert_page(query)
                    body = b"""<!doctype html><html><body>
                    <div class='results-count'>1 to 1 of 1 Results</div>
                    <div class='datasetResult'><a href='/dataset.xhtml?persistentId=doi%3A10.1234%2FTEST'>Local Dataset</a></div>
                    </body></html>"""
                    self.send_html(body)
                    return
                if split.path == "/dataset.xhtml":
                    host = f"http://127.0.0.1:{self.server.server_port}"
                    document = {
                        "@context": "https://schema.org",
                        "@type": "Dataset",
                        "identifier": "https://doi.org/10.1234/TEST",
                        "name": "Local Dataset",
                        "description": "Local integration fixture",
                        "distribution": [
                            {
                                "@type": "DataDownload",
                                "name": "package.zip",
                                "encodingFormat": "application/zip",
                                "contentSize": len(payload),
                                "contentUrl": f"{host}/files/package.zip",
                                "sha256": digest,
                            }
                        ],
                    }
                    body = (
                        "<!doctype html><html><head><script type='application/ld+json'>"
                        + json.dumps(document)
                        + "</script></head><body></body></html>"
                    ).encode()
                    self.send_html(body)
                    return
                if split.path == "/files/package.zip":
                    range_header = self.headers.get("Range")
                    if range_header:
                        start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                        chunk = payload[start:]
                        self.send_response(206)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                        self.end_headers()
                        self.wfile.write(chunk)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    return
                self.send_error(404)

            def assert_page(self, query: dict[str, list[str]]) -> None:
                if query.get("page") != ["1"]:
                    raise AssertionError(query)

            def send_html(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                base_url = f"http://127.0.0.1:{server.server_port}"
                settings = scraper.Settings(
                    base_url=base_url,
                    collection="pan",
                    source="html",
                    output_root=output,
                    delay_seconds=0,
                    requests_per_minute=0,
                    timeout_seconds=10,
                    max_retries=0,
                    max_pages=10,
                    max_records=1,
                    refresh=False,
                    download_files=True,
                    resume=True,
                    dry_run=False,
                    max_file_bytes=0,
                    min_free_bytes=0,
                    include_extensions=frozenset(),
                    exclude_extensions=frozenset(),
                    allow_external_downloads=False,
                    cookie_file=None,
                    user_agent="test-agent",
                )
                records = scraper.PanHtmlScraper(settings).run()
                self.assertEqual(1, len(records))
                catalog = json.loads((output / "catalog.json").read_text())
                self.assertFalse(catalog["source"]["metadata_api_used"])
                self.assertEqual(1, catalog["summary"]["completed_downloads"])
                record = records[0]
                local_path = output / record["files"][0]["local_path"]
                self.assertEqual(payload, local_path.read_bytes())
                self.assertTrue((output / record["raw"]["dataset_html"]).exists())
                self.assertTrue((output / record["raw"]["structured_data"]).exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ApiParserTests(unittest.TestCase):
    def test_search_page_yields_discovery_entries(self) -> None:
        document = json.loads((FIXTURES / "search_page_1.json").read_text())
        items, total = scraper.search_items(document)
        self.assertEqual(2, len(items))
        self.assertEqual(2, total)

        entry = scraper.discovery_from_search_item(
            items[0], "https://dataverse.harvard.edu", "search-page-url"
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual("doi:10.7910/DVN/AAAAAA", entry["persistent_id"])
        self.assertEqual("Dataset Alpha", entry["title"])
        self.assertEqual("search-page-url", entry["source_page"])

    def test_search_error_status_is_reported(self) -> None:
        with self.assertRaises(scraper.ScraperError):
            scraper.search_items({"status": "ERROR", "message": "bad subtree"})

    def test_croissant_export_yields_files_with_urls_and_checksums(self) -> None:
        document = json.loads((FIXTURES / "croissant_alpha.json").read_text())
        discovery = {
            "persistent_id": "doi:10.7910/DVN/AAAAAA",
            "landing_page": "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/AAAAAA",
            "title": "Dataset Alpha",
            "source_page": "search-page-url",
        }
        record, documents = scraper.parse_dataset_croissant(
            document, discovery, "https://dataverse.harvard.edu"
        )

        self.assertEqual("Dataset Alpha", record["title"])
        self.assertEqual("doi:10.7910/DVN/AAAAAA", record["persistent_id"])
        self.assertEqual("10.7910/DVN/AAAAAA", record["doi"])
        self.assertEqual(["Lovelace, Ada", "Hopper, Grace"], record["authors"])
        self.assertEqual("https://example.org/paper", record["paper_url"])
        self.assertEqual("dataverse_croissant_metadata_export", record["methods"]["record"]["label"])
        self.assertFalse(record["methods"]["download"]["url_construction"])
        self.assertEqual(1, len(documents))

        self.assertEqual(2, len(record["files"]))
        first = record["files"][0]
        self.assertEqual("replication.zip", first["name"])
        self.assertEqual("https://dataverse.harvard.edu/api/access/datafile/111", first["download_url"])
        self.assertEqual(1234, first["size_bytes"])
        self.assertEqual("0" * 32, first["checksums"]["md5"])

    def test_export_without_file_objects_yields_no_files(self) -> None:
        document = json.loads((FIXTURES / "croissant_beta_no_files.json").read_text())
        record, _ = scraper.parse_dataset_croissant(
            document,
            {"persistent_id": "doi:10.7910/DVN/BBBBBB", "landing_page": "", "title": "Dataset Beta"},
            "https://dataverse.harvard.edu",
        )
        self.assertEqual([], record["files"])
        # A citation with no URL must not become a fabricated paper link.
        self.assertEqual("", record["paper_url"])

    def test_download_redirect_to_object_storage_is_allowed(self) -> None:
        base = "https://dataverse.harvard.edu"
        storage = "https://dvn-cloud-iqss.s3.amazonaws.com/10.7910/DVN/X/abc?X-Amz-Signature=x"
        self.assertTrue(scraper.redirect_target_allowed(storage, base, False))
        self.assertTrue(scraper.redirect_target_allowed(f"{base}/api/access/datafile/1", base, False))
        self.assertFalse(scraper.redirect_target_allowed("https://evil.example.org/file.zip", base, False))
        self.assertTrue(scraper.redirect_target_allowed("https://evil.example.org/file.zip", base, True))


class ApiIntegrationTests(unittest.TestCase):
    def test_complete_local_api_inventory_and_download(self) -> None:
        payload = b"PK\x03\x04 fake zip payload for the api integration test"
        readme = b"readme bytes"
        search = json.loads((FIXTURES / "search_page_1.json").read_text())
        alpha = json.loads((FIXTURES / "croissant_alpha.json").read_text())
        # Point the fixture's file URLs and sizes at this test server.
        alpha["distribution"][0]["contentSize"] = str(len(payload))
        alpha["distribution"][0]["md5"] = hashlib.md5(payload).hexdigest()
        alpha["distribution"][1]["contentSize"] = str(len(readme))
        alpha["distribution"][1]["md5"] = hashlib.md5(readme).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                if parts.path == "/api/search":
                    return self.send_json(search)
                if parts.path == "/api/datasets/export":
                    pid = query.get("persistentId", [""])[0]
                    document = dict(alpha)
                    if "BBBBBB" in pid:
                        document = json.loads((FIXTURES / "croissant_beta_no_files.json").read_text())
                    else:
                        for node in document["distribution"]:
                            node["contentUrl"] = f"{self.server_base()}{urlsplit(node['contentUrl']).path}"
                    return self.send_json(document)
                if parts.path.startswith("/api/access/datafile/"):
                    body = readme if parts.path.endswith("112") else payload
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def server_base(self) -> str:
                return f"http://127.0.0.1:{self.server.server_port}"

            def send_json(self, document: object) -> None:
                body = json.dumps(document).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                base_url = f"http://127.0.0.1:{server.server_port}"
                settings = scraper.Settings(
                    base_url=base_url,
                    collection="pan",
                    source="api",
                    output_root=output,
                    delay_seconds=0,
                    requests_per_minute=0,
                    timeout_seconds=10,
                    max_retries=0,
                    max_pages=10,
                    max_records=None,
                    refresh=False,
                    download_files=True,
                    resume=True,
                    dry_run=False,
                    max_file_bytes=0,
                    min_free_bytes=0,
                    include_extensions=frozenset(),
                    exclude_extensions=frozenset(),
                    allow_external_downloads=False,
                    cookie_file=None,
                    user_agent="test-agent",
                )
                records = scraper.PanHtmlScraper(settings).run()

                self.assertEqual(2, len(records))
                catalog = json.loads((output / "catalog.json").read_text())
                self.assertTrue(catalog["source"]["metadata_api_used"])
                self.assertEqual("dataverse_search_and_croissant_export", catalog["source"]["method"])
                self.assertEqual(2, catalog["summary"]["completed_downloads"])

                alpha_record = next(r for r in records if r["persistent_id"].endswith("AAAAAA"))
                self.assertEqual(2, len(alpha_record["files"]))
                downloaded = output / alpha_record["files"][0]["local_path"]
                self.assertEqual(payload, downloaded.read_bytes())
                self.assertTrue((output / alpha_record["raw"]["metadata_export"]).exists())

                beta_record = next(r for r in records if r["persistent_id"].endswith("BBBBBB"))
                self.assertEqual([], beta_record["files"])

                missing = json.loads((output / "missing_paper_links.json").read_text())
                self.assertEqual(["doi:10.7910/DVN/BBBBBB"], [m["persistent_id"] for m in missing])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class RateLimitTests(unittest.TestCase):
    """Pacing and backpressure behaviour, exercised without real sleeping."""

    def make_client(self, *, delay: float, per_minute: int) -> "scraper.HtmlClient":
        settings = scraper.Settings(
            base_url="https://dataverse.harvard.edu",
            collection="pan",
            source="api",
            output_root=Path("."),
            delay_seconds=delay,
            requests_per_minute=per_minute,
            timeout_seconds=10,
            max_retries=2,
            max_pages=10,
            max_records=None,
            refresh=False,
            download_files=False,
            resume=False,
            dry_run=False,
            max_file_bytes=0,
            min_free_bytes=0,
            include_extensions=frozenset(),
            exclude_extensions=frozenset(),
            allow_external_downloads=False,
            cookie_file=None,
            user_agent="test-agent",
        )
        return scraper.HtmlClient(settings)

    def test_requests_per_minute_ceiling_is_enforced(self) -> None:
        client = self.make_client(delay=0, per_minute=30)
        clock = [1000.0]
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock[0] += seconds

        with unittest.mock.patch.object(scraper.time, "monotonic", lambda: clock[0]), unittest.mock.patch.object(
            scraper.time, "sleep", fake_sleep
        ):
            for _ in range(30):
                client._throttle()
            self.assertEqual([], slept)
            # The 31st request inside the same minute has to wait for the
            # oldest one to age out of the window.
            client._throttle()

        self.assertEqual(1, len(slept))
        self.assertAlmostEqual(60.0, slept[0], places=3)

    def test_delay_spaces_requests_even_without_a_ceiling(self) -> None:
        client = self.make_client(delay=1.5, per_minute=0)
        clock = [500.0]
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock[0] += seconds

        with unittest.mock.patch.object(scraper.time, "monotonic", lambda: clock[0]), unittest.mock.patch.object(
            scraper.time, "sleep", fake_sleep
        ):
            client._throttle()
            client._throttle()

        self.assertEqual([1.5], slept)

    def test_plain_403_is_treated_as_rate_limiting_and_widens_the_delay(self) -> None:
        client = self.make_client(delay=1.0, per_minute=40)
        response = unittest.mock.Mock()
        response.status_code = 403
        response.headers = {"Content-Type": "text/html"}

        self.assertTrue(scraper.HtmlClient._is_rate_limited(response))
        with unittest.mock.patch.object(scraper.time, "sleep"):
            wait = client._note_rate_limit(response)
        self.assertGreaterEqual(wait, scraper.RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertEqual(2.0, client._delay)

    def test_json_403_is_an_access_restriction_not_rate_limiting(self) -> None:
        response = unittest.mock.Mock()
        response.status_code = 403
        response.headers = {"Content-Type": "application/json;charset=UTF-8"}
        self.assertFalse(scraper.HtmlClient._is_rate_limited(response))

    def test_repeated_rate_limits_stop_the_crawl(self) -> None:
        calls = {"count": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                calls["count"] += 1
                body = b"<html><head><title>403 Forbidden</title></head></html>"
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self.make_client(delay=0, per_minute=0)
            # A fake clock keeps the widening backoff from actually sleeping;
            # the throttle re-checks monotonic(), so it has to advance too.
            clock = [0.0]

            def fake_sleep(seconds: float) -> None:
                clock[0] += seconds

            with unittest.mock.patch.object(scraper.time, "sleep", fake_sleep), unittest.mock.patch.object(
                scraper.time, "monotonic", lambda: clock[0]
            ):
                with self.assertRaises(scraper.RateLimitError):
                    client.request("GET", f"http://127.0.0.1:{server.server_port}/api/search")
            # It gives up after the circuit-break threshold rather than looping.
            self.assertEqual(scraper.RATE_LIMIT_CIRCUIT_BREAK, calls["count"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_success_after_backoff_relaxes_the_delay(self) -> None:
        client = self.make_client(delay=1.0, per_minute=40)
        client._delay = 8.0
        for _ in range(scraper.DELAY_DECAY_AFTER_SUCCESSES):
            client._note_success()
        self.assertLess(client._delay, 8.0)
        self.assertGreaterEqual(client._delay, 1.0)


if __name__ == "__main__":
    unittest.main()
