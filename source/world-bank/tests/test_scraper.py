from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from scraper import (
    BASE_URL,
    RateLimitedClient,
    discover_resources,
    download_resource,
    normalize_doi,
    normalize_record,
    parse_args,
    sanity_check,
)


class ResourceDiscoveryTests(unittest.TestCase):
    def test_deduplicates_hosted_resources_and_reads_display_size(self) -> None:
        html = """
        <a href="/catalog/305/download/918/package.zip"
           title="Get Reproducibility Package"></a>
        <a href="//reproducibility.worldbank.org/catalog/305/download/918"
           data-filename="RR_WLD_2025_394.zip"
           data-extension="zip" data-dctype="Program" data-isurl="0">
           Download [ZIP, 1.25 MB]
        </a>
        """
        result = discover_resources(
            html, "305", f"{BASE_URL}/catalog/305/related-materials"
        )

        self.assertEqual(len(result["hosted_resources"]), 1)
        resource = result["hosted_resources"][0]
        self.assertEqual(resource["resource_id"], "918")
        self.assertEqual(resource["filename"], "RR_WLD_2025_394.zip")
        self.assertEqual(resource["reported_size"], "1.25 MB")
        self.assertEqual(resource["reported_size_bytes_estimate"], 1_310_720)

    def test_records_external_resources_without_treating_them_as_hosted(self) -> None:
        html = """
        <a href="https://example.org/package.zip" data-isurl="1"
           data-filename="package.zip">External package</a>
        """
        result = discover_resources(
            html, "305", f"{BASE_URL}/catalog/305/related-materials"
        )

        self.assertEqual(result["hosted_resources"], [])
        self.assertEqual(len(result["external_resources"]), 1)
        self.assertEqual(
            result["external_resources"][0]["download"]["status"],
            "external_not_fetched",
        )

    def test_records_external_main_package_button(self) -> None:
        html = """
        <a href="https://github.com/example/package"
           title="Get Reproducibility Package">Get package</a>
        """
        result = discover_resources(
            html, "305", f"{BASE_URL}/catalog/305/related-materials"
        )

        self.assertEqual(result["hosted_resources"], [])
        self.assertEqual(len(result["external_resources"]), 1)
        self.assertEqual(
            result["external_resources"][0]["url"],
            "https://github.com/example/package",
        )


class PaperLinkTests(unittest.TestCase):
    def test_doi_normalization_handles_repository_field_variants(self) -> None:
        self.assertEqual(
            normalize_doi("10.1596/1813-9450-11176"),
            "https://doi.org/10.1596/1813-9450-11176",
        )
        self.assertEqual(
            normalize_doi("http://doi.org/10.1596/1813-9450-11176"),
            "https://doi.org/10.1596/1813-9450-11176",
        )
        self.assertEqual(
            normalize_doi("doi.org/10.1596/1813-9450-11176"),
            "https://doi.org/10.1596/1813-9450-11176",
        )
        self.assertEqual(
            normalize_doi("https://hdl.handle.net/10986/43438"),
            "https://hdl.handle.net/10986/43438",
        )
        self.assertEqual(normalize_doi("not a DOI"), "")

    def test_uri_has_global_priority_over_an_earlier_doi(self) -> None:
        detail = {
            "dataset": {
                "id": "1",
                "idno": "RR_TEST",
                "metadata": {
                    "project_desc": {
                        "output": [
                            {"title": "DOI first", "doi": "10.1234/first"},
                            {"title": "URI second", "uri": "https://example.org/paper"},
                        ]
                    }
                },
            }
        }

        record = normalize_record({"idno": "RR_TEST"}, detail, None, [])

        self.assertEqual(record["paper"]["title"], "URI second")
        self.assertEqual(record["paper"]["url"], "https://example.org/paper")
        self.assertEqual(record["paper"]["link_status"], "present")

    def test_absent_paper_link_is_an_empty_string(self) -> None:
        detail = {
            "dataset": {
                "id": "2",
                "idno": "RR_BLANK",
                "metadata": {"project_desc": {"output": [{"title": "No link"}]}},
            }
        }

        record = normalize_record({"idno": "RR_BLANK"}, detail, None, [])

        self.assertEqual(record["paper"]["url"], "")
        self.assertEqual(record["paper"]["link_status"], "absent")


class RequestBoundaryTests(unittest.TestCase):
    def test_rejects_external_and_non_https_urls(self) -> None:
        with self.assertRaises(ValueError):
            RateLimitedClient.validate_url("https://example.org/file.zip")
        with self.assertRaises(ValueError):
            RateLimitedClient.validate_url(
                "http://reproducibility.worldbank.org/catalog/1/download/2"
            )

    def test_retry_preserves_catalog_query_parameters(self) -> None:
        class FakeResponse:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                self.headers = {"Retry-After": "0"}

            def close(self) -> None:
                return None

            def raise_for_status(self) -> None:
                return None

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[dict[str, object] | None] = []
                self.responses = [FakeResponse(429), FakeResponse(200)]

            def get(self, _url: str, **kwargs: object) -> FakeResponse:
                params = kwargs.get("params")
                self.calls.append(params if isinstance(params, dict) else None)
                return self.responses.pop(0)

        client = RateLimitedClient(delay=0, max_retries=1)
        fake_session = FakeSession()
        client.session = fake_session  # type: ignore[assignment]

        response = client.get(
            f"{BASE_URL}/api/catalog",
            params={"page": 7, "ps": 100},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            fake_session.calls,
            [{"page": 7, "ps": 100}, {"page": 7, "ps": 100}],
        )

    def test_download_rejects_mismatched_catalog_id(self) -> None:
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            record_dir = data_dir / "RR_TEST"
            record_dir.mkdir(parents=True)
            resource = {
                "catalog_id": "305",
                "resource_id": "918",
                "filename": "package.zip",
                "extension": "zip",
                "url": f"{BASE_URL}/catalog/999/download/918",
                "download": {"status": "not_requested"},
            }

            with self.assertRaises(ValueError):
                download_resource(  # type: ignore[arg-type]
                    None, resource, record_dir, data_dir, None, 0
                )

    def test_download_rejects_symlinked_resource_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            record_dir = data_dir / "RR_TEST"
            files_dir = record_dir / "files"
            outside = root / "outside"
            files_dir.mkdir(parents=True)
            outside.mkdir()
            (files_dir / "918").symlink_to(outside, target_is_directory=True)
            resource = {
                "catalog_id": "305",
                "resource_id": "918",
                "filename": "package.zip",
                "extension": "zip",
                "url": f"{BASE_URL}/catalog/305/download/918",
                "download": {"status": "not_requested"},
            }

            with self.assertRaises(ValueError):
                download_resource(  # type: ignore[arg-type]
                    None, resource, record_dir, data_dir, None, 0
                )
            self.assertEqual(list(outside.iterdir()), [])

    def test_non_finite_numeric_options_are_rejected(self) -> None:
        for arguments in (["--delay", "nan"], ["--min-free-gb", "inf"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(arguments)


class FileSanityTests(unittest.TestCase):
    def test_rar_signature_is_checked(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "package.rar"
            path.write_bytes(b"not a rar")
            self.assertFalse(sanity_check(path, "rar")["ok"])
            path.write_bytes(b"Rar!\x1a\x07\x01\x00payload")
            self.assertTrue(sanity_check(path, "rar")["ok"])


if __name__ == "__main__":
    unittest.main()
