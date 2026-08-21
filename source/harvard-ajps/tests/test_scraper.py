from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scraper  # noqa: E402


SEARCH_ITEM = {
    "name": "Replication Data for: An Example AJPS Article",
    "type": "dataset",
    "global_id": "doi:10.7910/DVN/ABC123",
    "published_at": "2025-01-02T03:04:05Z",
    "majorVersion": 2,
    "minorVersion": 1,
    "entity_id": 99,
}


def primitive(type_name: str, value):
    return {
        "typeName": type_name,
        "multiple": isinstance(value, list),
        "typeClass": "primitive",
        "value": value,
    }


def compound(type_name: str, values):
    return {
        "typeName": type_name,
        "multiple": True,
        "typeClass": "compound",
        "value": values,
    }


def subfield(type_name: str, value):
    return {
        "typeName": type_name,
        "multiple": False,
        "typeClass": "primitive",
        "value": value,
    }


def sample_api_response() -> dict:
    return {
        "status": "OK",
        "data": {
            "id": 70102,
            "datasetId": 52887,
            "datasetPersistentId": "doi:10.7910/DVN/ABC123",
            "versionNumber": 2,
            "versionMinorNumber": 1,
            "versionState": "RELEASED",
            "createTime": "2025-01-01T00:00:00Z",
            "lastUpdateTime": "2025-01-02T00:00:00Z",
            "releaseTime": "2025-01-02T00:00:00Z",
            "publicationDate": "2025-01-02",
            "license": {"name": "CC0 1.0", "uri": "https://creativecommons.org/publicdomain/zero/1.0/"},
            "metadataBlocks": {
                "citation": {
                    "fields": [
                        primitive("title", "Replication Data for: An Example AJPS Article"),
                        compound(
                            "author",
                            [
                                {
                                    "authorName": subfield("authorName", "Example, Ada"),
                                    "authorAffiliation": subfield(
                                        "authorAffiliation", "Example University"
                                    ),
                                    "authorIdentifierScheme": subfield(
                                        "authorIdentifierScheme", "ORCID"
                                    ),
                                    "authorIdentifier": subfield(
                                        "authorIdentifier", "0000-0000-0000-0000"
                                    ),
                                }
                            ],
                        ),
                        compound(
                            "datasetContact",
                            [
                                {
                                    "datasetContactName": subfield(
                                        "datasetContactName", "Example, Ada"
                                    ),
                                    "datasetContactEmail": subfield(
                                        "datasetContactEmail", "ada@example.test"
                                    ),
                                }
                            ],
                        ),
                        compound(
                            "dsDescription",
                            [
                                {
                                    "dsDescriptionValue": subfield(
                                        "dsDescriptionValue", "Code and data."
                                    )
                                }
                            ],
                        ),
                        primitive("subject", ["Social Sciences"]),
                        compound(
                            "keyword",
                            [{"keywordValue": subfield("keywordValue", "replication")}],
                        ),
                        primitive("language", ["English"]),
                        compound(
                            "publication",
                            [
                                {
                                    "publicationCitation": subfield(
                                        "publicationCitation", "Example. 2025. AJPS."
                                    ),
                                    "publicationIDType": subfield(
                                        "publicationIDType", "doi"
                                    ),
                                    "publicationIDNumber": subfield(
                                        "publicationIDNumber", "10.1111/ajps.12345"
                                    ),
                                    "publicationURL": subfield(
                                        "publicationURL",
                                        "https://doi.org/10.1111/ajps.12345",
                                    ),
                                },
                                {},
                            ],
                        ),
                        primitive(
                            "relatedDataset",
                            ["Companion data: https://example.test/companion"],
                        ),
                    ]
                }
            },
            "files": [
                {
                    "label": "analysis.tab",
                    "directoryLabel": "../untrusted",
                    "description": "Analysis data",
                    "restricted": False,
                    "categories": ["Data"],
                    "dataFile": {
                        "id": 42,
                        "persistentId": "doi:10.7910/DVN/ABC123/FILE01",
                        "filename": "analysis.tab",
                        "contentType": "text/tab-separated-values",
                        "filesize": 30,
                        "originalFileName": "analysis.dta",
                        "originalFileFormat": "application/x-stata",
                        "originalFileSize": 3,
                        "checksum": {
                            "type": "MD5",
                            "value": hashlib.md5(b"abc").hexdigest(),
                        },
                    },
                },
                {
                    "label": "copyright.pdf",
                    "restricted": True,
                    "dataFile": {
                        "id": 43,
                        "filename": "copyright.pdf",
                        "contentType": "application/pdf",
                        "filesize": 4,
                        "checksum": {
                            "type": "MD5",
                            "value": hashlib.md5(b"pdf!").hexdigest(),
                        },
                    },
                },
            ],
        },
    }


def normalized_record(download_format: str = "original") -> dict:
    return scraper.normalize_record(
        sample_api_response(),
        SEARCH_ITEM,
        base_url="https://dataverse.harvard.edu",
        collection_alias="ajps",
        policy_url="https://ajps.org/ajps-verification-policy/",
        discovery_url="https://dataverse.harvard.edu/api/search?q=%2A",
        metadata_url="https://dataverse.harvard.edu/api/datasets/example",
        download_format=download_format,
        expected_version="2.1",
        harvested_at="2026-08-21T10:00:00Z",
    )


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers=None, url="https://storage.test/file"):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class FakeDownloadClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, url: str, *, headers=None):
        self.calls.append((url, dict(headers or {})))
        if not self.responses:
            raise AssertionError(f"Unexpected network call: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAPIClient:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def get_json(self, url: str):
        self.calls.append(url)
        if not self.routes:
            raise AssertionError(f"Unexpected network call: {url}")
        predicate, payload = self.routes.pop(0)
        if not predicate(url):
            raise AssertionError(f"Unexpected URL: {url}")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return scraper.JsonResponse(payload, body, url, url, 200, {})


class NormalizationTests(unittest.TestCase):
    def test_version_and_persistent_id_from_search_item(self):
        self.assertEqual(scraper.version_from_search_item(SEARCH_ITEM), "2.1")
        self.assertEqual(
            scraper.persistent_id_from_search_item(SEARCH_ITEM),
            "doi:10.7910/DVN/ABC123",
        )
        fallback = {
            "url": "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi%3A10.1%2FX"
        }
        self.assertEqual(scraper.persistent_id_from_search_item(fallback), "doi:10.1/X")

    def test_storage_keys_are_collision_resistant(self):
        left = scraper.storage_key("doi:10.1/A/B")
        right = scraper.storage_key("doi:10.1/A:B")
        self.assertNotEqual(left, right)
        self.assertRegex(left, r"^[A-Za-z0-9._-]+$")

    def test_safe_filename_drops_paths_and_controls(self):
        self.assertEqual(scraper.safe_filename("../../evil.zip"), "evil.zip")
        self.assertEqual(scraper.safe_filename(r"C:\\tmp\\evil.zip"), "evil.zip")
        self.assertEqual(scraper.safe_filename("\x00\n.."), "download")
        self.assertEqual(scraper.safe_filename("CON"), "_CON")

    def test_embargo_and_external_url_safety(self):
        today = dt.date(2026, 8, 21)
        self.assertTrue(
            scraper.embargo_is_active({"dateAvailable": "2026-08-22"}, today=today)
        )
        self.assertFalse(
            scraper.embargo_is_active({"dateAvailable": "2026-08-21"}, today=today)
        )
        self.assertEqual(
            scraper.extract_urls(
                "keep https://example.test/x reject https://user:secret@example.test/y"
            ),
            ["https://example.test/x"],
        )

    def test_record_normalization_preserves_original_and_archival(self):
        record = normalized_record()
        self.assertEqual(record["record_id"], "doi:10.7910/DVN/ABC123")
        self.assertEqual(record["version"]["number"], "2.1")
        self.assertEqual(record["version"]["version_id"], 70102)
        self.assertIsNone(record["verification_status"])
        self.assertEqual(record["authors"][0]["name"], "Example, Ada")
        self.assertEqual(record["paper"]["identifier"], "10.1111/ajps.12345")
        self.assertEqual(
            record["external_links"],
            [{"url": "https://example.test/companion", "followed": False}],
        )

        file_record = record["hosted_files"][0]
        self.assertEqual(file_record["filename"], "analysis.dta")
        self.assertEqual(file_record["size_bytes"], 3)
        self.assertEqual(file_record["archival_representation"]["filename"], "analysis.tab")
        self.assertEqual(file_record["original_representation"]["filename"], "analysis.dta")
        self.assertIn("format=original", file_record["urls"]["download"])
        self.assertNotIn("untrusted", file_record["download"]["local_path"])
        self.assertEqual(
            record["hosted_files"][1]["download"]["status"], "skipped_restricted"
        )

    def test_archival_mode_uses_archival_name_and_size(self):
        file_record = normalized_record("archival")["hosted_files"][0]
        self.assertEqual(file_record["filename"], "analysis.tab")
        self.assertEqual(file_record["size_bytes"], 30)
        self.assertNotIn("format=original", file_record["urls"]["download"])
        self.assertIsNone(file_record["checksum"])

    def test_version_mismatch_is_rejected(self):
        with self.assertRaisesRegex(scraper.HarvesterError, "Pinned version mismatch"):
            scraper.normalize_record(
                sample_api_response(),
                SEARCH_ITEM,
                base_url="https://dataverse.harvard.edu",
                collection_alias="ajps",
                policy_url="https://ajps.org/ajps-verification-policy/",
                discovery_url="https://dataverse.harvard.edu/api/search",
                metadata_url="https://dataverse.harvard.edu/api/datasets/example",
                download_format="original",
                expected_version="9.9",
            )

    def test_malformed_file_marks_record_partial(self):
        response = sample_api_response()
        response["data"]["files"].append({"label": "missing-id.txt", "dataFile": {}})
        record = scraper.normalize_record(
            response,
            SEARCH_ITEM,
            base_url="https://dataverse.harvard.edu",
            collection_alias="ajps",
            policy_url="https://ajps.org/ajps-verification-policy/",
            discovery_url="https://dataverse.harvard.edu/api/search",
            metadata_url="https://dataverse.harvard.edu/api/datasets/example",
            download_format="original",
            expected_version="2.1",
        )
        self.assertEqual(record["harvest_status"], "partial")
        self.assertRegex(record["normalization_warnings"][0], "dataFile.id")

    def test_sha512_checksum_is_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "file.bin"
            path.write_bytes(b"abc")
            file_record = {
                "size_bytes": 3,
                "checksum": {
                    "type": "SHA-512",
                    "value": hashlib.sha512(b"abc").hexdigest(),
                },
            }
            self.assertEqual(scraper.verify_file(path, file_record), (True, True, None))

    def test_summary_is_recomputed_from_records(self):
        record = normalized_record()
        summary = scraper.summarize_records(
            [record],
            source_total=833,
            discovered_count=1,
            limited=True,
            errors_this_run=0,
        )
        self.assertEqual(summary["records_complete"], 1)
        self.assertEqual(summary["hosted_file_count"], 2)
        self.assertEqual(summary["public_file_count"], 1)
        self.assertEqual(summary["restricted_file_count"], 1)
        self.assertEqual(summary["estimated_public_download_bytes"], 3)
        self.assertEqual(summary["records_with_paper_links"], 1)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output_data = Path(self.temporary.name) / "data"
        self.output_data.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def public_file(self):
        return normalized_record()["hosted_files"][0]

    def destination(self, file_record):
        return self.output_data / Path(file_record["download"]["local_path"]).relative_to("data")

    def test_download_streams_verifies_and_atomically_renames(self):
        file_record = self.public_file()
        client = FakeDownloadClient([FakeResponse(b"abc")])
        scraper.download_file(
            client,
            file_record,
            output_dir=self.output_data,
            resume=False,
            max_bytes=None,
            min_free_bytes=0,
        )
        destination = self.destination(file_record)
        self.assertEqual(destination.read_bytes(), b"abc")
        self.assertFalse(destination.with_name(destination.name + ".part").exists())
        self.assertEqual(file_record["download"]["status"], "downloaded")
        self.assertTrue(file_record["download"]["checksum_verified"])

    def test_resume_appends_only_on_valid_206(self):
        file_record = self.public_file()
        destination = self.destination(file_record)
        destination.parent.mkdir(parents=True)
        destination.with_name(destination.name + ".part").write_bytes(b"a")
        response = FakeResponse(
            b"bc",
            status=206,
            headers={"Content-Length": "2", "Content-Range": "bytes 1-2/3"},
        )
        client = FakeDownloadClient([response])
        scraper.download_file(
            client,
            file_record,
            output_dir=self.output_data,
            resume=True,
            max_bytes=None,
            min_free_bytes=0,
        )
        self.assertEqual(destination.read_bytes(), b"abc")
        self.assertEqual(client.calls[0][1]["Range"], "bytes=1-")

    def test_resume_restarts_when_server_returns_200(self):
        file_record = self.public_file()
        destination = self.destination(file_record)
        destination.parent.mkdir(parents=True)
        destination.with_name(destination.name + ".part").write_bytes(b"wrong")
        client = FakeDownloadClient([FakeResponse(b"abc", status=200)])
        scraper.download_file(
            client,
            file_record,
            output_dir=self.output_data,
            resume=True,
            max_bytes=None,
            min_free_bytes=0,
        )
        self.assertEqual(destination.read_bytes(), b"abc")

    def test_invalid_content_range_fails_closed(self):
        file_record = self.public_file()
        destination = self.destination(file_record)
        destination.parent.mkdir(parents=True)
        part = destination.with_name(destination.name + ".part")
        part.write_bytes(b"a")
        client = FakeDownloadClient(
            [
                FakeResponse(
                    b"bc",
                    status=206,
                    headers={"Content-Length": "2", "Content-Range": "bytes 0-1/3"},
                )
            ]
        )
        scraper.download_file(
            client,
            file_record,
            output_dir=self.output_data,
            resume=True,
            max_bytes=None,
            min_free_bytes=0,
        )
        self.assertEqual(file_record["download"]["status"], "failed")
        self.assertFalse(destination.exists())
        self.assertTrue(part.exists())

    def test_truncated_response_is_not_finalized(self):
        file_record = self.public_file()
        client = FakeDownloadClient(
            [FakeResponse(b"ab", headers={"Content-Length": "3"})]
        )
        scraper.download_file(
            client,
            file_record,
            output_dir=self.output_data,
            resume=False,
            max_bytes=None,
            min_free_bytes=0,
        )
        destination = self.destination(file_record)
        self.assertEqual(file_record["download"]["status"], "failed")
        self.assertFalse(destination.exists())
        self.assertTrue(destination.with_name(destination.name + ".part").exists())

    def test_limits_and_restrictions_make_no_network_calls(self):
        public = self.public_file()
        client = FakeDownloadClient([])
        scraper.download_file(
            client,
            public,
            output_dir=self.output_data,
            resume=False,
            max_bytes=2,
            min_free_bytes=0,
        )
        self.assertEqual(public["download"]["status"], "skipped_max_size")

        restricted = normalized_record()["hosted_files"][1]
        scraper.download_file(
            client,
            restricted,
            output_dir=self.output_data,
            resume=False,
            max_bytes=None,
            min_free_bytes=0,
        )
        self.assertEqual(restricted["download"]["status"], "skipped_restricted")
        self.assertEqual(client.calls, [])

    def test_existing_valid_file_is_not_downloaded_again(self):
        file_record = self.public_file()
        destination = self.destination(file_record)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"abc")
        client = FakeDownloadClient([])
        scraper.download_file(
            client,
            file_record,
            output_dir=self.output_data,
            resume=True,
            max_bytes=None,
            min_free_bytes=0,
        )
        self.assertEqual(file_record["download"]["status"], "already_present")
        self.assertEqual(client.calls, [])


class IntegrationTests(unittest.TestCase):
    def test_offline_inventory_writes_matching_record_and_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "inventory"
            args = scraper.parse_args(
                ["--inventory-only", "--output-dir", str(output), "--per-page", "1"]
            )
            search_payload = {
                "status": "OK",
                "data": {"total_count": 1, "start": 0, "count_in_response": 1, "items": [SEARCH_ITEM]},
            }
            client = FakeAPIClient(
                [
                    (lambda url: "/api/search?" in url, search_payload),
                    (lambda url: "/versions/2.1?" in url, sample_api_response()),
                ]
            )
            harvester = scraper.AJPSHarvester(args, client)
            with contextlib.redirect_stdout(io.StringIO()):
                result = harvester.run()
            self.assertEqual(result, 0)
            catalog = scraper.read_json(output / "catalog.json")
            record = scraper.read_json(
                output
                / "data"
                / scraper.storage_key("doi:10.7910/DVN/ABC123")
                / "record.json"
            )
            self.assertEqual(catalog["records"], [record])
            self.assertEqual(catalog["summary"]["source_total_records"], 1)
            self.assertEqual(catalog["summary"]["records_complete"], 1)
            raw_search = output / "data" / "raw" / "search" / "page_000001.json"
            self.assertEqual(json.loads(raw_search.read_text()), search_payload)
            parsed_search = urllib.parse.urlparse(client.calls[0])
            query = urllib.parse.parse_qs(parsed_search.query)
            self.assertEqual(query["subtree"], ["ajps"])
            self.assertEqual(query["fq"], ["publicationStatus:Published"])
            self.assertEqual(query["q"], ["*"])

    def test_smoke_mode_is_isolated_and_limited(self):
        args = scraper.parse_args(["--smoke-test"])
        self.assertEqual(args.output_dir, Path("smoke-output"))
        self.assertEqual(args.max_records, 3)
        self.assertFalse(args.download_files)

    def test_cached_record_must_match_pinned_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "inventory"
            args = scraper.parse_args(
                ["--inventory-only", "--resume", "--output-dir", str(output)]
            )
            harvester = scraper.AJPSHarvester(args, FakeAPIClient([]))
            record = normalized_record()
            record_path, _ = harvester._record_paths(record["persistent_id"])
            scraper.atomic_write_json(record_path, record)
            self.assertIsNotNone(harvester._cached_record(record["persistent_id"], "2.1"))
            self.assertIsNone(harvester._cached_record(record["persistent_id"], "3.0"))


if __name__ == "__main__":
    unittest.main()
