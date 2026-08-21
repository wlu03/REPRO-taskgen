from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zenodo_community_harvester.model import (
    catalog_summary,
    community_summary,
    merge_download_state,
    normalize_record,
    search_hits,
    search_next,
    search_total,
)
from zenodo_community_harvester.util import ensure_within, html_to_text, safe_component, safe_filename, stable_file_id


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SearchTests(unittest.TestCase):
    def test_current_search_shape(self):
        payload = fixture("search_page.json")
        self.assertEqual("21725672", search_hits(payload)[0]["id"])
        self.assertEqual(1, search_total(payload))
        self.assertEqual("", search_next(payload))

    def test_legacy_array_shape(self):
        payload = [{"id": 1}, {"id": 2}]
        self.assertEqual(2, len(search_hits(payload)))
        self.assertEqual(2, search_total(payload))

    def test_total_object_shape(self):
        payload = {"hits": {"hits": [], "total": {"value": 42}}}
        self.assertEqual(42, search_total(payload))


class NormalizationTests(unittest.TestCase):
    def normalized(self):
        record = fixture("record.json")
        record["files"] = fixture("files.json")
        community = community_summary(fixture("community.json"), "ej-replication-repository", "https://zenodo.org")
        return normalize_record(
            record,
            community=community,
            base_url="https://zenodo.org",
            raw_response_path="data/21725672/api_response.json",
            files_response_path="data/21725672/files_response.json",
            discovery_response_path="data/raw/search/page-000001.json",
        )

    def test_identifiers_are_not_interchanged(self):
        record = self.normalized()
        self.assertEqual("21725672", record["identifiers"]["record_id"])
        self.assertEqual("21725671", record["identifiers"]["concept_record_id"])
        self.assertEqual("10.5281/zenodo.21725672", record["identifiers"]["doi"])
        self.assertEqual("10.5281/zenodo.21725671", record["identifiers"]["concept_doi"])

    def test_file_inventory_uses_exact_content_links(self):
        record = self.normalized()
        first, second = record["hosted_files"]
        self.assertEqual("c65a3fb9-0223-4dd4-b260-eaf7c0d0b848", first["file_id"])
        self.assertTrue(first["download_url"].endswith("/README.pdf/content"))
        self.assertEqual(
            "https://zenodo.org/api/records/21725672/files/nested%2F3%20replication%20package.zip/content",
            second["download_url"],
        )
        self.assertNotIn("nested/", second["filename"])

    def test_paper_and_external_links(self):
        record = self.normalized()
        self.assertEqual("https://doi.org/10.1093/ej/example", record["paper_url"])
        self.assertEqual(2, len(record["external_links"]))

    def test_modern_rights_props_url_is_preserved(self):
        record = self.normalized()
        self.assertEqual(
            "https://creativecommons.org/licenses/by/4.0/legalcode",
            record["licenses"][0]["link"],
        )

    def test_description_has_html_and_plain_text(self):
        record = self.normalized()
        self.assertIn("<strong>", record["description_html"])
        self.assertEqual("Dutcher, E. Glenn and Krista J. Saral (2026). Remote work .", record["description_text"])

    def test_record_summary(self):
        record = self.normalized()
        summary = catalog_summary([record], 0)
        self.assertEqual(1, summary["record_count"])
        self.assertEqual(2, summary["hosted_file_count"])
        self.assertEqual(46980245, summary["estimated_download_bytes"])

    def test_download_state_merges_by_exact_key(self):
        record = self.normalized()
        previous = {
            "hosted_files": [
                {
                    "key": "README.pdf",
                    "size_bytes": 67149,
                    "checksum": "md5:556b3320c8fc4b779300f5c2de552a16",
                    "status": "downloaded",
                    "downloaded_bytes": 67149,
                }
            ]
        }
        merged = merge_download_state(record, previous)
        self.assertEqual("downloaded", merged["hosted_files"][0]["status"])
        self.assertEqual("not_requested", merged["hosted_files"][1]["status"])

    def test_changed_file_metadata_invalidates_old_download_state(self):
        record = self.normalized()
        previous = {
            "hosted_files": [
                {
                    "key": "README.pdf",
                    "size_bytes": 1,
                    "checksum": "md5:00000000000000000000000000000000",
                    "status": "downloaded",
                    "downloaded_bytes": 1,
                }
            ]
        }
        merged = merge_download_state(record, previous)
        self.assertEqual("not_requested", merged["hosted_files"][0]["status"])

    def test_modern_file_self_link_is_not_mistaken_for_content(self):
        record = fixture("record.json")
        record["files"] = {
            "enabled": True,
            "entries": [
                {
                    "key": "restricted.zip",
                    "file_id": "file-uuid",
                    "size": 12,
                    "links": {"self": "https://zenodo.org/api/records/21725672/files/restricted.zip"},
                }
            ],
        }
        record["access"]["files"] = "restricted"
        community = community_summary(fixture("community.json"), "ej-replication-repository", "https://zenodo.org")
        normalized = normalize_record(
            record,
            community=community,
            base_url="https://zenodo.org",
            raw_response_path="data/21725672/api_response.json",
            files_response_path="data/21725672/files_response.json",
            discovery_response_path="data/raw/search/page-000001.json",
        )
        file_entry = normalized["hosted_files"][0]
        self.assertEqual("", file_entry["download_url"])
        self.assertFalse(file_entry["downloadable"])
        self.assertTrue(file_entry["restricted"])

    def test_nested_hidden_file_is_never_downloadable(self):
        record = fixture("record.json")
        record["files"] = {
            "enabled": True,
            "entries": [
                {
                    "key": "owner-only.zip",
                    "file_id": "hidden-file-uuid",
                    "size": 12,
                    "access": {"hidden": True},
                    "links": {
                        "self": "https://zenodo.org/api/records/21725672/files/owner-only.zip",
                        "content": "https://zenodo.org/api/records/21725672/files/owner-only.zip/content",
                    },
                }
            ],
        }
        community = community_summary(fixture("community.json"), "ej-replication-repository", "https://zenodo.org")
        normalized = normalize_record(
            record,
            community=community,
            base_url="https://zenodo.org",
            raw_response_path="data/21725672/api_response.json",
            files_response_path="data/21725672/files_response.json",
            discovery_response_path="data/raw/search/page-000001.json",
        )
        file_entry = normalized["hosted_files"][0]
        self.assertTrue(file_entry["hidden"])
        self.assertTrue(file_entry["restricted"])
        self.assertFalse(file_entry["downloadable"])

    def test_legacy_embargoed_access_never_makes_a_content_link_downloadable(self):
        record = fixture("record.json")
        record.pop("access", None)
        record["access_right"] = "embargoed"
        record["files"] = fixture("files.json")
        community = community_summary(fixture("community.json"), "ej-replication-repository", "https://zenodo.org")
        normalized = normalize_record(
            record,
            community=community,
            base_url="https://zenodo.org",
            raw_response_path="data/21725672/api_response.json",
            files_response_path="data/21725672/files_response.json",
            discovery_response_path="data/raw/search/page-000001.json",
        )
        self.assertTrue(normalized["hosted_files"])
        self.assertTrue(all(item["restricted"] for item in normalized["hosted_files"]))
        self.assertTrue(all(not item["downloadable"] for item in normalized["hosted_files"]))


class PathSafetyTests(unittest.TestCase):
    def test_untrusted_names_become_safe_components(self):
        self.assertEqual("escape.zip", safe_filename("../escape.zip"))
        self.assertEqual("escape.zip", safe_filename("..\\windows\\escape.zip"))
        self.assertNotIn("/", safe_component("a/b"))
        self.assertTrue(stable_file_id("a/b.zip").startswith("f-"))

    def test_candidate_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                ensure_within(root, root / ".." / "escape")

    def test_html_to_text_does_not_execute_or_keep_tags(self):
        self.assertEqual("Hello world", html_to_text("<p>Hello <script>world</script></p>"))


if __name__ == "__main__":
    unittest.main()
