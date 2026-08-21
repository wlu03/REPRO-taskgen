import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scraper import Harvester, ScraperError, build_parser, normalize_record, validate_args


FIXTURES = Path(__file__).parent / "fixtures"
PRIOR_GENERATED_AT = "2025-01-02T03:04:05+00:00"
PRIOR_REGISTER_SHA256 = "1" * 64


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def second_record(entry, detail):
    second_entry = copy.deepcopy(entry)
    second_detail = copy.deepcopy(detail)
    second_entry["Certificate ID"] = "2025-022"
    second_entry["Certificate Link"] = (
        "https://codecheck.org.uk/register/certs/2025-022/"
    )
    second_entry["Certificate PDF"] = (
        "https://zenodo.org/api/records/16616999/files/codecheck.pdf/content"
    )
    second_entry["Title"] = "Second current register title"
    second_detail["certificate"]["id"] = "2025-022"
    second_detail["certificate"]["url"] = second_entry["Certificate Link"]
    second_detail["paper"]["title"] = second_entry["Title"]
    return second_entry, second_detail


def partial_args(root):
    parser = build_parser()
    args = parser.parse_args(
        [
            "--inventory-only",
            "--offline",
            "--certificate-id",
            "2025-021",
            "--output",
            str(root),
            "--min-free-gb",
            "0",
        ]
    )
    validate_args(args, parser)
    return args


def prepare_offline_run(root):
    entry = load_fixture("register.json")[0]
    detail = load_fixture("certificate.json")
    other_entry, other_detail = second_record(entry, detail)
    write_json(root / "data" / "raw" / "register.json", [entry, other_entry])
    write_json(root / "data" / "raw" / "register-full.json", [])
    write_json(root / "data" / "raw" / ".meta.json", {})
    write_json(
        root / "data" / "2025-021" / "certificate_response.json",
        detail,
    )
    return entry, detail, other_entry, other_detail


class PartialCatalogTests(unittest.TestCase):
    def test_valid_prior_catalog_preserves_records_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            entry, detail, other_entry, other_detail = prepare_offline_run(root)
            prior_processed = normalize_record(entry, detail)
            prior_processed["title"] = "Stale title that must be replaced"
            prior_preserved = normalize_record(other_entry, other_detail)
            prior_preserved["title"] = "Preserved prior title"
            prior_catalog = {
                "schema_version": "1.0",
                "generated_at": PRIOR_GENERATED_AT,
                "software": {
                    "name": "codecheck-register-scraper",
                    "version": "1.0.0",
                },
                "source": {
                    "register": {"sha256": PRIOR_REGISTER_SHA256},
                },
                "records": [prior_processed, prior_preserved],
            }
            write_json(root / "catalog.json", prior_catalog)
            write_json(root / "data" / "2025-021" / "record.json", prior_processed)
            write_json(root / "data" / "2025-022" / "record.json", prior_preserved)

            harvester = Harvester(partial_args(root))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    catalog = harvester.run()
            finally:
                harvester.close()

            self.assertEqual(
                [record["certificate_id"] for record in catalog["records"]],
                ["2025-021", "2025-022"],
            )
            self.assertNotEqual(
                catalog["records"][0]["title"],
                prior_processed["title"],
            )
            self.assertEqual(catalog["records"][1], prior_preserved)
            self.assertEqual(catalog["summary"]["records"], 2)
            self.assertEqual(
                catalog["run_scope"]["processed_certificate_ids"],
                ["2025-021"],
            )
            self.assertEqual(
                catalog["run_scope"]["preserved_certificate_ids"],
                ["2025-022"],
            )
            self.assertEqual(catalog["run_scope"]["preserved_prior_records"], 1)
            self.assertEqual(catalog["run_scope"]["unrepresented_certificate_ids"], [])
            self.assertEqual(
                catalog["run_scope"]["preserved_record_provenance"],
                {
                    "catalog_generated_at": PRIOR_GENERATED_AT,
                    "register_sha256": PRIOR_REGISTER_SHA256,
                },
            )

    def test_malformed_prior_catalog_fails_closed_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            prepare_offline_run(root)
            malformed_catalog = {
                "schema_version": "1.0",
                "software": {"name": "codecheck-register-scraper"},
                "records": [{"title": "missing certificate ID"}],
            }
            original_bytes = json.dumps(malformed_catalog).encode("utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_bytes(original_bytes)

            harvester = Harvester(partial_args(root))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ScraperError, "malformed catalog"):
                        harvester.run()
            finally:
                harvester.close()

            self.assertEqual(catalog_path.read_bytes(), original_bytes)

    def test_matching_but_incomplete_prior_record_also_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            prepare_offline_run(root)
            malformed_catalog = {
                "schema_version": "1.0",
                "generated_at": PRIOR_GENERATED_AT,
                "software": {"name": "codecheck-register-scraper"},
                "source": {"register": {"sha256": PRIOR_REGISTER_SHA256}},
                "records": [
                    {
                        "schema_version": "1.0",
                        "certificate_id": "2025-022",
                        "paper": {},
                        "check": {},
                        "repository": {},
                        "report": {},
                        "artifacts": "not-a-list",
                        "methods": [],
                        "errors": [],
                    }
                ],
            }
            original_bytes = json.dumps(malformed_catalog).encode("utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_bytes(original_bytes)

            harvester = Harvester(partial_args(root))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ScraperError, "malformed catalog"):
                        harvester.run()
            finally:
                harvester.close()

            self.assertEqual(catalog_path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
