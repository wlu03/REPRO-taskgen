import json
import tempfile
import unittest
from pathlib import Path

from scraper import (
    Harvester,
    ScraperError,
    build_parser,
    carry_forward_repository_artifacts,
    merge_previous_artifact_state,
    normalize_record,
    registered_certificate_artifact,
    validate_args,
)


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_args(output, *extra):
    parser = build_parser()
    args = parser.parse_args([*extra, "--output", str(output), "--min-free-gb", "0"])
    validate_args(args, parser)
    return args


class RecordingDownloader:
    def __init__(self):
        self.calls = []

    def download(self, artifact, artifact_dir):
        self.calls.append((artifact, artifact_dir))


class CarryForwardTests(unittest.TestCase):
    def test_repository_metadata_and_artifacts_are_carried_together(self):
        descriptor = "github::example/project"
        previous = {
            "repository": {
                "descriptor": descriptor,
                "provider": "github",
                "license": "MIT",
                "revision": "a" * 40,
                "default_branch": "main",
            },
            "artifacts": [
                {
                    "artifact_id": "repository-archive",
                    "role": "repository_archive",
                    "provider": "github",
                    "source_url": "https://api.github.com/repos/example/project/zipball/abc",
                    "status": "downloaded",
                    "local_path": "data/id/files/repository/archive/project.zip",
                },
                {
                    "artifact_id": "certificate",
                    "role": "registered_certificate_artifact",
                    "source_url": "https://files.example/certificate.pdf",
                },
            ],
        }
        current = {
            "repository": {
                "descriptor": descriptor,
                "provider": "github",
                "license": None,
                "revision": None,
            },
            "artifacts": [],
        }

        carry_forward_repository_artifacts(current, previous)

        self.assertEqual(current["repository"]["license"], "MIT")
        self.assertEqual(current["repository"]["revision"], "a" * 40)
        self.assertEqual(current["repository"]["default_branch"], "main")
        self.assertEqual(
            current["repository"]["metadata_freshness"],
            "carried_forward_not_resolved_this_run",
        )
        self.assertEqual([item["artifact_id"] for item in current["artifacts"]], ["repository-archive"])
        self.assertEqual(
            current["artifacts"][0]["metadata_freshness"],
            "carried_forward_not_resolved_this_run",
        )

    def test_provider_filter_does_not_download_carried_artifact(self):
        entry = load_fixture("register.json")[0]
        detail = load_fixture("certificate.json")
        previous = normalize_record(entry, detail)
        previous["artifacts"] = [
            {
                "artifact_id": "old-github-archive",
                "role": "repository_archive",
                "provider": "github",
                "source_url": "https://api.github.com/repos/example/project/zipball/abc",
                "filename": "project.zip",
                "size_bytes": None,
                "sha256": None,
                "status": "discovered",
                "local_path": None,
            }
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            harvester = Harvester(
                make_args(root, "--download-repositories", "--provider", "zenodo")
            )
            record_dir = root / "data" / "2025-021"
            record_dir.mkdir(parents=True)
            (record_dir / "certificate_response.json").write_text(
                json.dumps(detail), encoding="utf-8"
            )
            (record_dir / "record.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )
            recorder = RecordingDownloader()
            harvester.downloader = recorder
            try:
                record = harvester._process_record(entry, None)
            finally:
                harvester.close()

        self.assertEqual(recorder.calls, [])
        self.assertIn(
            "old-github-archive",
            [item["artifact_id"] for item in record["artifacts"]],
        )
        self.assertIn(
            "provider_filter",
            [method.get("label") for method in record["methods"]],
        )


class ResearchEqualsStateTests(unittest.TestCase):
    def test_resolution_failure_does_not_fall_back_to_retired_url(self):
        entry = load_fixture("register.json")[0]
        detail = load_fixture("certificate.json")
        entry["Certificate PDF"] = (
            "https://www.researchequals.com/api/modules/main/"
            "8d0a10a6-c84b-4505-903a-f55224441a40"
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            harvester = Harvester(make_args(root, "--download-certificates"))
            record_dir = root / "data" / "2025-021"
            record_dir.mkdir(parents=True)
            (record_dir / "certificate_response.json").write_text(
                json.dumps(detail), encoding="utf-8"
            )
            harvester.resolver.resolve_researchequals_certificate = (
                lambda artifact, response_dir, certificate_id: (_ for _ in ()).throw(
                    ScraperError("version is not published")
                )
            )
            recorder = RecordingDownloader()
            harvester.downloader = recorder
            try:
                record = harvester._process_record(entry, None)
            finally:
                harvester.close()

        self.assertEqual(recorder.calls, [])
        self.assertEqual(
            [error["stage"] for error in record["errors"]],
            ["certificate_artifact_resolution"],
        )

    def test_researchequals_resolution_provenance_is_carried_forward(self):
        entry = load_fixture("register.json")[0]
        detail = load_fixture("certificate.json")
        entry["Certificate PDF"] = (
            "https://www.researchequals.com/api/modules/main/"
            "8d0a10a6-c84b-4505-903a-f55224441a40"
        )
        record = normalize_record(entry, detail)
        fresh = registered_certificate_artifact(record)
        previous_artifact = {
            **fresh,
            "registered_source_url": fresh["source_url"],
            "download_url": "https://researchequals.com/api/files/content-id",
            "resolution_method": "researchequals_v2_version_api",
            "resolution_url": (
                "https://researchequals.com/api/versions/"
                "8d0a10a6-c84b-4505-903a-f55224441a40"
            ),
            "provider_revision": "8d0a10a6-c84b-4505-903a-f55224441a40",
            "provider_license_id": "Q20007257",
            "provider_metadata": {"published": True, "content_s3": "content-id"},
            "status": "downloaded",
            "local_path": "data/2025-021/files/certificate/id/certificate.pdf",
        }

        merge_previous_artifact_state([fresh], {"artifacts": [previous_artifact]})

        for field in (
            "registered_source_url",
            "download_url",
            "resolution_method",
            "resolution_url",
            "provider_revision",
            "provider_license_id",
            "provider_metadata",
        ):
            self.assertEqual(fresh[field], previous_artifact[field])


if __name__ == "__main__":
    unittest.main()
