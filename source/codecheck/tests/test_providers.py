import json
import tempfile
import unittest
from pathlib import Path

from scraper import ProviderResolver
from scraper import ScraperError


FIXTURES = Path(__file__).parent / "fixtures"


class StubHttp:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def fetch_json(self, url, cache_path, **kwargs):
        self.urls.append(url)
        for marker, payload in self.responses:
            if marker in url:
                return payload, {"url": url, "cache": "stub"}
        raise AssertionError(f"Unexpected URL: {url}")


class ProviderTests(unittest.TestCase):
    def test_github_archive_is_pinned_to_commit(self):
        http = StubHttp(
            [
                (
                    "/commits/main",
                    {"sha": "a" * 40},
                ),
                (
                    "/repos/codecheckers/example",
                    {
                        "private": False,
                        "default_branch": "main",
                        "license": {"spdx_id": "MIT"},
                    },
                ),
            ]
        )
        resolver = ProviderResolver(http, refresh=False, github_token=None)
        repository = {
            "provider": "github",
            "locator": "codecheckers/example",
            "subpath": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = resolver.resolve(repository, Path(temporary).resolve())
        self.assertEqual(repository["revision"], "a" * 40)
        self.assertEqual(repository["license"], "MIT")
        self.assertIn("/zipball/" + "a" * 40, artifacts[0]["source_url"])
        self.assertEqual(artifacts[0]["discovery_method"], "github_repository_api_archive")

    def test_github_internal_visibility_is_rejected(self):
        http = StubHttp(
            [
                (
                    "/repos/codecheckers/example",
                    {
                        "private": False,
                        "visibility": "internal",
                        "default_branch": "main",
                    },
                )
            ]
        )
        resolver = ProviderResolver(http, refresh=False, github_token="token")
        repository = {
            "provider": "github",
            "locator": "codecheckers/example",
            "subpath": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ScraperError):
                resolver.resolve(repository, Path(temporary).resolve())

    def test_zenodo_uses_content_links_and_preserves_relative_name(self):
        payload = json.loads((FIXTURES / "zenodo.json").read_text())
        http = StubHttp([("/api/records/15025861", payload)])
        resolver = ProviderResolver(http, refresh=False, github_token=None)
        repository = {"provider": "zenodo", "locator": "15025861", "subpath": None}
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = resolver.resolve(repository, Path(temporary).resolve())
        self.assertEqual(repository["license"], "cc-by-4.0")
        self.assertEqual(artifacts[0]["relative_path"], "code/data.csv")
        self.assertEqual(
            artifacts[0]["source_url"],
            payload["files"][0]["links"]["content"],
        )

    def test_zenodo_restricted_access_is_rejected(self):
        payload = json.loads((FIXTURES / "zenodo.json").read_text())
        payload["access"] = {"status": "restricted"}
        http = StubHttp([("/api/records/15025861", payload)])
        resolver = ProviderResolver(http, refresh=False, github_token=None)
        repository = {"provider": "zenodo", "locator": "15025861", "subpath": None}
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ScraperError):
                resolver.resolve(repository, Path(temporary).resolve())

    def test_osf_recurses_public_storage_files(self):
        root = json.loads((FIXTURES / "osf_root.json").read_text())
        responses = [
            (
                "/v2/licenses/mit/",
                {
                    "data": {
                        "id": "mit",
                        "attributes": {"name": "MIT License"},
                    }
                },
            ),
            ("/nodes/abc12/children/", {"data": [], "links": {"next": None}}),
            (
                "/nodes/abc12/files/",
                {
                    "data": [
                        {
                            "attributes": {"provider": "osfstorage", "name": "osfstorage"},
                            "relationships": {
                                "files": {
                                    "links": {
                                        "related": {"href": "https://api.osf.io/v2/files/root/"}
                                    }
                                }
                            },
                        }
                    ],
                    "links": {"next": None},
                },
            ),
            (
                "/v2/files/root/",
                {
                    "data": [
                        {
                            "id": "file-1",
                            "attributes": {
                                "kind": "file",
                                "name": "analysis.R",
                                "size": 12,
                                "extra": {"hashes": {"sha256": "f" * 64}},
                            },
                            "links": {"download": "https://files.osf.io/v1/resources/abc12/analysis.R"},
                        }
                    ],
                    "links": {"next": None},
                },
            ),
            ("/nodes/abc12/", root),
        ]
        resolver = ProviderResolver(StubHttp(responses), refresh=False, github_token=None)
        repository = {"provider": "osf", "locator": "abc12", "subpath": None}
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = resolver.resolve(repository, Path(temporary).resolve())
        self.assertEqual(repository["license"], "MIT License")
        self.assertEqual(artifacts[0]["filename"], "analysis.R")
        self.assertEqual(artifacts[0]["source_checksum"], "sha256:" + "f" * 64)

    def test_researchequals_legacy_route_resolves_through_v2(self):
        version_id = "8d0a10a6-c84b-4505-903a-f55224441a40"
        content_id = "fbdeef1a-eec8-46bb-9131-939a5e8d4f52"
        payload = {
            "id": version_id,
            "output_id": "output-id",
            "version": 1,
            "version_label": "1.0.0",
            "published": True,
            "content_s3": content_id,
            "content_mediatype": "application/pdf",
            "license_id": "Q20007257",
        }
        http = StubHttp([(f"/api/versions/{version_id}", payload)])
        resolver = ProviderResolver(http, refresh=False, github_token=None)
        artifact = {
            "source_url": f"https://www.researchequals.com/api/modules/main/{version_id}",
            "filename": "certificate.bin",
        }
        with tempfile.TemporaryDirectory() as temporary:
            resolved = resolver.resolve_researchequals_certificate(
                artifact,
                Path(temporary).resolve(),
                "2020-007",
            )
        self.assertTrue(resolved)
        self.assertEqual(
            artifact["download_url"],
            f"https://researchequals.com/api/files/{content_id}",
        )
        self.assertEqual(artifact["filename"], "certificate-2020-007.pdf")
        self.assertEqual(artifact["resolution_method"], "researchequals_v2_version_api")


if __name__ == "__main__":
    unittest.main()
