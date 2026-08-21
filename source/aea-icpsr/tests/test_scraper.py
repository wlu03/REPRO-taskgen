from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scraper  # noqa: E402


def search_page(docs: list[dict], total: int) -> str:
    payload = {
        "response": {"docs": docs, "numFound": total, "start": 0},
        "responseHeader": {"params": {"fq": ["ARCHIVE:aea"]}},
    }
    return (
        "<html><script>ReactDOM.render(React.createElement("
        "SearchPage, {searchResults : "
        + json.dumps(payload)
        + ", searchConfig : {}}), node);</script></html>"
    )


class FakeDiscoveryClient:
    def __init__(self, pages: dict[int, tuple[list[dict], int]]) -> None:
        self.pages = pages
        self.requested_starts: list[int] = []

    def get_search_html(self, url: str) -> str:
        query = parse_qs(urlsplit(url).query)
        start = int(query["start"][0])
        self.requested_starts.append(start)
        docs, total = self.pages[start]
        return search_page(docs, total)


class FakeBibliographyClient:
    def __init__(self, data=None, error: Exception | None = None) -> None:
        self.data = [] if data is None else data
        self.error = error

    def get_json(self, url: str, *, role: str):
        if self.error is not None:
            raise self.error
        return self.data


class FakeDownloadResponse:
    def __init__(self, body: bytes, content_type: str = "application/zip") -> None:
        self.body = body
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": content_type,
        }
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeDownloadClient:
    def __init__(self, response: FakeDownloadResponse, final_url: str = "") -> None:
        self.response = response
        self.final_url = final_url

    def open_download(self, url: str):
        return self.response, self.final_url or url


class FlakySession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise scraper.requests.ConnectionError("temporary reset")
        response = mock.Mock()
        response.status_code = 200
        return response

    def close(self) -> None:
        pass


class FakeBrowserSession:
    """Stands in for AeaIcpsrBrowserSession without launching Chromium."""

    def __init__(self, state: str = "ready", payload: bytes | None = None,
                 result: dict | None = None) -> None:
        self.state = state
        self.payload = payload
        self.result = result
        self.cleared_urls: list[str] = []
        self.downloaded_urls: list[str] = []

    def ensure_clearance(self, url: str) -> str:
        self.cleared_urls.append(url)
        return self.state

    def download(self, url: str, destination) -> dict:
        self.downloaded_urls.append(url)
        if self.payload is not None:
            destination.write_bytes(self.payload)
        return self.result or {"status": "complete"}


class AeaIcpsrScraperTests(unittest.TestCase):
    def test_connection_error_is_retried(self) -> None:
        client = scraper.AeaIcpsrClient(delay=0, max_retries=1)
        flaky = FlakySession()
        client.session.close()
        client.session = flaky
        with mock.patch.object(scraper.time, "sleep"):
            response = client.request(
                "GET",
                "https://search.icpsr.umich.edu/search/search/studies",
                role="search",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(flaky.calls, 2)
        client.close()

    def test_embedded_json_parser_handles_nested_braces(self) -> None:
        doc = {
            "ID": 120506,
            "TITLE": "A title with {braces}",
            "SUMMARY": ["<div>{nested}</div>"],
            "ARCHIVE": ["aea"],
        }
        parsed = scraper.parse_embedded_search_results(search_page([doc], 1))
        self.assertEqual(parsed["response"]["numFound"], 1)
        self.assertEqual(parsed["response"]["docs"][0]["ID"], 120506)

    def test_discovery_paginates_and_deduplicates(self) -> None:
        first = [
            {"ID": 1, "ARCHIVE": ["aea"]},
            {"ID": 2, "ARCHIVE": ["aea"]},
        ]
        second = [{"ID": 3, "ARCHIVE": ["aea"]}]
        client = FakeDiscoveryClient({0: (first, 3), 2: (second, 3)})
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            records, total = scraper.discover_records(
                client, cache, page_size=2, max_records=None, study_id=None, resume=False
            )
        self.assertEqual(total, 3)
        self.assertEqual([record["ID"] for record in records], [1, 2, 3])
        self.assertEqual(client.requested_starts, [0, 2])

    def test_discovery_rejects_silently_truncated_full_catalog(self) -> None:
        first = [
            {"ID": 1, "ARCHIVE": ["aea"]},
            {"ID": 2, "ARCHIVE": ["aea"]},
        ]
        second = [
            {"ID": 2, "ARCHIVE": ["aea"]},
            {"ID": 3, "ARCHIVE": ["aea"]},
        ]
        client = FakeDiscoveryClient({0: (first, 4), 2: (second, 4)})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "partial catalog"):
                scraper.discover_records(
                    client,
                    Path(temporary),
                    page_size=2,
                    max_records=None,
                    study_id=None,
                    resume=False,
                )

    def test_project_url_uses_the_discovered_version(self) -> None:
        parsed = scraper.parse_project_url(
            "https://www.openicpsr.org/openicpsr/project/120506/version/V3/view",
            "120506",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["version"], "V3")
        self.assertEqual(parsed["filename"], "120506-V3.zip")
        self.assertIn("fcr%3Aversions%2FV3", parsed["package_url"])

    def test_project_url_rejects_mismatched_id_and_host(self) -> None:
        self.assertIsNone(
            scraper.parse_project_url(
                "https://www.openicpsr.org/openicpsr/project/9/version/V1/view",
                "10",
            )
        )
        self.assertIsNone(
            scraper.parse_project_url(
                "https://example.com/openicpsr/project/10/version/V1/view", "10"
            )
        )

    def test_summary_extracts_text_and_links_as_resources_only(self) -> None:
        source = {
            "URL": "https://www.openicpsr.org/openicpsr/project/120506/version/V1/view",
            "SUMMARY": [
                "<p>A summary.</p>"
                '<a href="https://doi.org/10.3886/E120506V1">package</a>'
                '<a href="https://doi.org/10.1257/aer.20190848">article</a>'
            ],
        }
        text, links = scraper.parse_summary(source)
        self.assertIn("A summary.", text)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[1]["url"], "https://doi.org/10.1257/aer.20190848")

    def test_unchecked_paper_link_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paper, fetch, errors, related = scraper.fetch_paper_metadata(
                FakeBibliographyClient(),
                "120506",
                Path(temporary),
                fetch_requested=False,
                resume=False,
                previous_record={},
            )
        self.assertEqual(paper["url"], "")
        self.assertEqual(paper["link_status"], "not_checked")
        self.assertEqual(fetch["status"], "not_requested")
        self.assertEqual(errors, [])
        self.assertEqual(related, [])

    def test_successful_bibliography_lookup_confirms_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paper, fetch, errors, related = scraper.fetch_paper_metadata(
                FakeBibliographyClient([]),
                "120506",
                Path(temporary),
                fetch_requested=True,
                resume=False,
                previous_record={},
            )
            self.assertTrue((Path(temporary) / "related_publications.json").exists())
        self.assertEqual(paper["url"], "")
        self.assertEqual(paper["link_status"], "absent")
        self.assertEqual(fetch["status"], "complete")
        self.assertEqual(errors, [])
        self.assertEqual(related, [])

    def test_blocked_bibliography_lookup_is_fetch_error_not_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paper, fetch, errors, related = scraper.fetch_paper_metadata(
                FakeBibliographyClient(error=scraper.AeaIcpsrAccessBlocked("challenge")),
                "120506",
                Path(temporary),
                fetch_requested=True,
                resume=False,
                previous_record={},
            )
        self.assertEqual(paper["url"], "")
        self.assertEqual(paper["link_status"], "fetch_error")
        self.assertEqual(fetch["status"], "access_blocked")
        self.assertEqual(len(errors), 1)
        self.assertEqual(related, [])

    def test_failed_refresh_preserves_previous_verified_paper(self) -> None:
        previous_citations = [{"type": "article-journal", "title": "Paper"}]
        previous = {
            "paper": {
                "title": "Paper",
                "authors": "Author",
                "citation": "Citation",
                "url": "https://doi.org/10.1257/example.1",
                "url_source": "bibliography_api",
                "link_status": "present",
                "outputs": [],
            },
            "related_publications": previous_citations,
        }
        with tempfile.TemporaryDirectory() as temporary:
            paper, fetch, errors, related = scraper.fetch_paper_metadata(
                FakeBibliographyClient(error=scraper.AeaIcpsrAccessBlocked("challenge")),
                "120506",
                Path(temporary),
                fetch_requested=True,
                resume=False,
                previous_record=previous,
            )
        self.assertEqual(paper, previous["paper"])
        self.assertEqual(related, previous_citations)
        self.assertEqual(fetch["status"], "access_blocked")
        self.assertTrue(fetch["preserved_previous_verified_value"])
        self.assertEqual(len(errors), 1)

    def test_bibliography_api_prefers_article_doi_and_keeps_metadata(self) -> None:
        citations = [
            {
                "type": "dataset",
                "title": "Package",
                "DOI": "10.3886/E120506V1",
            },
            {
                "type": "article-journal",
                "title": "Going Negative at the Zero Lower Bound",
                "DOI": "10.1257/aer.20190848",
                "author": [{"given": "Mauricio", "family": "Ulate"}],
                "clobs": {"htmlCitation": "<div>Ulate. <i>AER</i>.</div>"},
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            paper, fetch, errors, related = scraper.fetch_paper_metadata(
                FakeBibliographyClient(citations),
                "120506",
                Path(temporary),
                fetch_requested=True,
                resume=False,
                previous_record={},
            )
        self.assertEqual(paper["link_status"], "present")
        self.assertEqual(paper["url"], "https://doi.org/10.1257/aer.20190848")
        self.assertEqual(paper["authors"], "Mauricio Ulate")
        self.assertEqual(paper["url_source"], "bibliography_api")
        self.assertEqual(fetch["citations_found"], 2)
        self.assertEqual(related, citations)

    def test_download_streams_and_verifies_zip(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("README.txt", "hello")
        body = archive.getvalue()
        response = FakeDownloadResponse(body)
        resource = {
            "resource_id": "120506-V1",
            "filename": "120506-V1.zip",
            "url": "https://www.openicpsr.org/example.zip",
            "download": {"status": "not_requested"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "120506"
            record_dir.mkdir()
            result = scraper.download_package(
                FakeDownloadClient(
                    response,
                    "https://openicpsr-data.s3.amazonaws.com/package.zip?"
                    "X-Amz-Credential=secret#fragment",
                ),
                resource,
                record_dir,
                max_bytes=None,
                min_free_bytes=0,
            )
            package = record_dir / "files" / "120506-V1.zip"
            self.assertTrue(package.is_file())
            self.assertTrue(zipfile.is_zipfile(package))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["bytes"], len(body))
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(
            result["final_url"],
            "https://openicpsr-data.s3.amazonaws.com/package.zip",
        )
        self.assertTrue(response.closed)

    def test_html_download_is_never_saved_as_zip(self) -> None:
        response = FakeDownloadResponse(
            b"<!doctype html><html><title>Sign in</title></html>", "text/html"
        )
        resource = {
            "resource_id": "10-V1",
            "filename": "10-V1.zip",
            "url": "https://www.openicpsr.org/example.zip",
            "download": {"status": "not_requested"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "10"
            record_dir.mkdir()
            result = scraper.download_package(
                FakeDownloadClient(response), resource, record_dir, None, 0
            )
            self.assertFalse((record_dir / "files" / "10-V1.zip").exists())
        self.assertEqual(result["status"], "auth_required")

    def test_empty_download_is_failed(self) -> None:
        response = FakeDownloadResponse(b"")
        resource = {
            "resource_id": "10-V1",
            "filename": "10-V1.zip",
            "url": "https://www.openicpsr.org/example.zip",
            "download": {"status": "not_requested"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "10"
            record_dir.mkdir()
            result = scraper.download_package(
                FakeDownloadClient(response), resource, record_dir, None, 0
            )
        self.assertEqual(result["status"], "failed")

    def test_summary_counts_are_embedded_at_catalog_top(self) -> None:
        records = [
            {
                "version": "V1",
                "paper": {"link_status": "present"},
                "resources": [
                    {
                        "kind": "project_archive",
                        "download": {"status": "complete", "bytes": 12},
                    }
                ],
                "errors": [],
            },
            {
                "version": "V2",
                "paper": {"link_status": "absent"},
                "resources": [
                    {
                        "kind": "project_archive",
                        "download": {"status": "not_requested"},
                    }
                ],
                "errors": ["one"],
            },
        ]
        document = scraper.build_catalog_document(records, 6058, "aea_archive")
        summary = document["summary"]
        self.assertEqual(summary["reported_catalog_total"], 6058)
        self.assertEqual(summary["paper_links_present"], 1)
        self.assertEqual(summary["paper_links_missing"], 1)
        self.assertEqual(summary["downloaded_bytes"], 12)
        self.assertEqual(document["records"], records)

    # -- browser-backed downloads -----------------------------------------

    def _browser_resource(self) -> dict:
        return {
            "resource_id": "120506-V1",
            "filename": "120506-V1.zip",
            "url": "https://www.openicpsr.org/example.zip",
            "download": {"status": "not_requested"},
        }

    def test_browser_download_saves_and_hashes_the_package(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("README.txt", "hello")
        body = archive.getvalue()
        session = FakeBrowserSession(payload=body)
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "120506"
            record_dir.mkdir()
            result = scraper.download_package_via_browser(
                session,
                self._browser_resource(),
                record_dir,
                max_bytes=None,
                min_free_bytes=0,
                project_url="https://www.openicpsr.org/project/120506/view",
            )
            package = record_dir / "files" / "120506-V1.zip"
            self.assertTrue(package.is_file())
            self.assertTrue(zipfile.is_zipfile(package))
            self.assertFalse(list(package.parent.glob(".*.part")))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["bytes"], len(body))
        self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["transport"], "browser")
        self.assertEqual(
            session.cleared_urls,
            ["https://www.openicpsr.org/project/120506/view"],
        )

    def test_browser_download_reports_unsolved_challenge(self) -> None:
        session = FakeBrowserSession(state="challenge")
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "120506"
            record_dir.mkdir()
            result = scraper.download_package_via_browser(
                session, self._browser_resource(), record_dir,
                max_bytes=None, min_free_bytes=0, project_url="https://x/view",
            )
            self.assertEqual(list((record_dir / "files").iterdir()), [])
        self.assertEqual(result["status"], "access_blocked")
        self.assertEqual(session.downloaded_urls, [])

    def test_browser_download_maps_terms_and_login_states(self) -> None:
        for state, expected in (("terms", "terms_required"), ("login", "auth_required")):
            with tempfile.TemporaryDirectory() as temporary:
                record_dir = Path(temporary) / "120506"
                record_dir.mkdir()
                result = scraper.download_package_via_browser(
                    FakeBrowserSession(state=state), self._browser_resource(),
                    record_dir, max_bytes=None, min_free_bytes=0,
                    project_url="https://x/view",
                )
            self.assertEqual(result["status"], expected)

    def test_browser_download_rejects_non_zip_and_clears_partial(self) -> None:
        session = FakeBrowserSession(payload=b"<html>terms</html>")
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "120506"
            record_dir.mkdir()
            with self.assertRaises(RuntimeError):
                scraper.download_package_via_browser(
                    session, self._browser_resource(), record_dir,
                    max_bytes=None, min_free_bytes=0, project_url="https://x/view",
                )
            files_dir = record_dir / "files"
            self.assertEqual(list(files_dir.iterdir()), [])

    def test_browser_download_enforces_max_file_size(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("README.txt", "hello" * 100)
        session = FakeBrowserSession(payload=archive.getvalue())
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "120506"
            record_dir.mkdir()
            with self.assertRaises(RuntimeError):
                scraper.download_package_via_browser(
                    session, self._browser_resource(), record_dir,
                    max_bytes=10, min_free_bytes=0, project_url="https://x/view",
                )
            self.assertEqual(list((record_dir / "files").iterdir()), [])

    def test_browser_download_skips_when_disk_is_low(self) -> None:
        session = FakeBrowserSession(payload=b"zzz")
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "120506"
            record_dir.mkdir()
            result = scraper.download_package_via_browser(
                session, self._browser_resource(), record_dir,
                max_bytes=None, min_free_bytes=1 << 62,
                project_url="https://x/view",
            )
        self.assertEqual(result["status"], "skipped_low_space")
        self.assertEqual(session.cleared_urls, [])


if __name__ == "__main__":
    unittest.main()
