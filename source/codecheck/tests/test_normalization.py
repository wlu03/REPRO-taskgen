import json
import tempfile
import unittest
from pathlib import Path

from scraper import (
    ScraperError,
    normalize_record,
    parse_repository_reference,
    registered_certificate_artifact,
    safe_relative_path,
    sanitize_segment,
)


FIXTURES = Path(__file__).parent / "fixtures"


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.entry = json.loads((FIXTURES / "register.json").read_text())[0]
        self.detail = json.loads((FIXTURES / "certificate.json").read_text())

    def test_normalizes_register_and_detail(self):
        record = normalize_record(self.entry, self.detail)
        self.assertEqual(record["certificate_id"], "2025-021")
        self.assertEqual(record["repository"]["provider"], "github")
        self.assertEqual(record["repository"]["locator"], "codecheckers/certificate-2025-021")
        self.assertEqual(record["paper"]["authors"][0]["orcid"], "0009-0006-3918-4240")
        self.assertEqual(record["check"]["date"], "2025-07-30")
        self.assertEqual(record["check"]["manifest"][0]["file"], "flight_Va_predict.pdf")
        self.assertEqual(record["methods"][0]["request_ref"], "../raw/register.json")

    def test_missing_optional_fields_are_preserved_as_empty(self):
        minimal = dict(self.entry)
        minimal["OpenAlex"] = None
        record = normalize_record(minimal, None)
        self.assertIsNone(record["paper"]["openalex_url"])
        self.assertEqual(record["paper"]["authors"], [])
        self.assertIsNone(record["paper"]["abstract"])

    def test_full_register_is_used_as_detail_fallback(self):
        full = {
            "Certificate ID": "2025-021",
            "Paper authors": [{"name": "Fallback Author"}],
            "Codecheckers": [{"name": "Fallback Checker"}],
            "Summary": "Fallback summary",
            "Source": "Dataset at https://example.org and code at https://example.com/repo",
        }
        record = normalize_record(self.entry, None, full)
        self.assertEqual(record["paper"]["authors"][0]["name"], "Fallback Author")
        self.assertEqual(record["check"]["codecheckers"][0]["name"], "Fallback Checker")
        self.assertEqual(record["check"]["summary"], "Fallback summary")
        self.assertEqual(record["check"]["source_note"], full["Source"])

    def test_registered_artifact_uses_exact_upstream_url(self):
        record = normalize_record(self.entry, self.detail)
        artifact = registered_certificate_artifact(record)
        self.assertEqual(artifact["source_url"], self.entry["Certificate PDF"])
        self.assertEqual(artifact["filename"], "codecheck.pdf")
        self.assertEqual(artifact["role"], "registered_certificate_artifact")

    def test_repository_reference_with_subpath(self):
        ref = parse_repository_reference("github::org/repo|reports/08")
        self.assertEqual(ref.provider, "github")
        self.assertEqual(ref.locator, "org/repo")
        self.assertEqual(ref.subpath, "reports/08")

    def test_range_identifier_is_safe_as_folder(self):
        self.assertEqual(sanitize_segment("2025-111/2025-222"), "2025-111__2025-222")

    def test_unicode_filename_fits_filesystem_byte_limit(self):
        value = sanitize_segment("界" * 100 + ".txt")
        self.assertLessEqual(len(value.encode("utf-8")), 240)
        self.assertTrue(value.endswith(".txt"))

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ScraperError):
            safe_relative_path(["code/../../secret"])

    def test_mismatched_detail_id_is_rejected(self):
        detail = json.loads(json.dumps(self.detail))
        detail["certificate"]["id"] = "1999-999"
        with self.assertRaises(ScraperError):
            normalize_record(self.entry, detail)


if __name__ == "__main__":
    unittest.main()
