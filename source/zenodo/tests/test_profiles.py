from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from zenodo.cli import _selected_profiles, build_parser, main
from zenodo.platform_catalog import write_platform_catalog
from zenodo.profiles import all_profiles, custom_profile, resolve_profile


FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = {
    "the-economic-journal": "ej-replication-repository",
    "restud": "restud-replication",
    "econometric-society": "es-replication-repository",
    "jeea": "jeea_replication",
}


class MultiCommunityFixtureHandler(BaseHTTPRequestHandler):
    requested_community_paths: set[str] = set()

    def do_GET(self):
        path = urlsplit(self.path).path
        expected_accept = "application/json" if path.endswith("/files") else "application/vnd.inveniordm.v1+json"
        if self.headers.get("Accept") != expected_accept:
            self.send_error(406)
            return
        payload = None
        for profile in all_profiles():
            community_path = f"/api/communities/{profile.slug}"
            if path == community_path:
                self.requested_community_paths.add(path)
                community = json.loads((FIXTURES / "community.json").read_text(encoding="utf-8"))
                community["id"] = profile.slug
                community["slug"] = profile.slug
                community["metadata"]["title"] = {"en": profile.title}
                payload = json.dumps(community).encode("utf-8")
                break
            if path == f"{community_path}/records":
                self.requested_community_paths.add(path)
                payload = (FIXTURES / "search_page.json").read_bytes()
                break
        if path == "/api/records/21725672":
            payload = (FIXTURES / "record.json").read_bytes()
        elif path == "/api/records/21725672/files":
            payload = (FIXTURES / "files.json").read_bytes()
        if payload is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class ProfileRegistryTests(unittest.TestCase):
    def test_exact_supported_slugs(self):
        self.assertEqual(EXPECTED, {profile.key: profile.slug for profile in all_profiles()})

    def test_aliases_resolve_and_jeea_underscore_is_preserved(self):
        self.assertEqual("the-economic-journal", resolve_profile("EJ").key)
        self.assertEqual("restud", resolve_profile("review_of_economic_studies").key)
        self.assertEqual("econometric-society", resolve_profile("ES").key)
        self.assertEqual("jeea_replication", resolve_profile("JEEA").slug)

    def test_profile_keys_slugs_and_aliases_are_unique(self):
        profiles = all_profiles()
        self.assertEqual(len(profiles), len({profile.key for profile in profiles}))
        self.assertEqual(len(profiles), len({profile.slug for profile in profiles}))
        owners = {}
        for profile in profiles:
            for alias in (profile.key, profile.slug, profile.abbreviation, *profile.aliases):
                normalized = alias.lower().replace("_", "-")
                previous = owners.get(normalized)
                if previous is not None:
                    self.assertEqual(profile.key, previous)
                owners[normalized] = profile.key

    def test_custom_profile_keeps_valid_slug_and_rejects_unsafe_value(self):
        self.assertEqual("my_community", custom_profile("my_community").slug)
        with self.assertRaises(ValueError):
            custom_profile("../../escape")

    def test_journal_and_community_flags_conflict_at_parse_time(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["--journal", "restud", "--community", "custom"])
        self.assertEqual(2, raised.exception.code)

    def test_no_selector_defaults_to_ej_and_profile_alias_is_supported(self):
        parser = build_parser()
        self.assertEqual("the-economic-journal", _selected_profiles(parser.parse_args([]), parser)[0].key)
        self.assertEqual("restud", _selected_profiles(parser.parse_args(["--profile", "ReStud"]), parser)[0].key)

    def test_unknown_profile_fails_clearly(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            _selected_profiles(parser.parse_args(["--journal", "not-a-profile"]), parser)
        self.assertEqual(2, raised.exception.code)

    def test_all_journals_rejects_a_single_community_output_root(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            (Path(temp) / "data").mkdir()
            with self.assertRaises(SystemExit) as raised:
                main(["--all-journals", "--output", temp])
            self.assertEqual(2, raised.exception.code)

    def test_all_journals_paces_between_community_engines(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch("zenodo.cli.run", return_value=0) as run_mock, mock.patch(
            "zenodo.cli.write_platform_catalog"
        ), mock.patch("zenodo.cli.time.sleep") as sleep_mock:
            code = main(["--all-journals", "--output", temp, "--delay", "2.1"])
            self.assertEqual(0, code)
            self.assertEqual(4, run_mock.call_count)
            self.assertEqual([mock.call(2.1), mock.call(2.1), mock.call(2.1)], sleep_mock.call_args_list)


class AllProfilesIntegrationTests(unittest.TestCase):
    def test_all_profiles_use_one_engine_and_isolated_outputs(self):
        MultiCommunityFixtureHandler.requested_community_paths = set()
        server = ThreadingHTTPServer(("127.0.0.1", 0), MultiCommunityFixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
                port = server.server_address[1]
                code = main(
                    [
                        "--all-journals",
                        "--inventory-only",
                        "--base-url",
                        f"http://127.0.0.1:{port}",
                        "--output",
                        temp,
                        "--delay",
                        "0",
                        "--retries",
                        "0",
                        "--min-free-gb",
                        "0",
                    ]
                )
                self.assertEqual(0, code)
                root = Path(temp)
                platform_catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
                self.assertEqual(4, platform_catalog["summary"]["community_count"])
                self.assertEqual(4, platform_catalog["summary"]["record_count"])
                self.assertEqual(4, platform_catalog["summary"]["unique_community_record_count"])
                self.assertTrue(platform_catalog["run"]["complete"])
                self.assertFalse(platform_catalog["run"]["truncated_by_max_records"])
                for key, slug in EXPECTED.items():
                    child_catalog = json.loads((root / key / "catalog.json").read_text(encoding="utf-8"))
                    child_record = json.loads((root / key / "data" / "21725672" / "record.json").read_text(encoding="utf-8"))
                    self.assertEqual(child_record, child_catalog["records"][0])
                    self.assertEqual(key, child_record["collection"]["key"])
                    self.assertEqual(slug, child_record["collection"]["slug"])
                    self.assertIn(f"/api/communities/{slug}", MultiCommunityFixtureHandler.requested_community_paths)
                    self.assertIn(f"/api/communities/{slug}/records", MultiCommunityFixtureHandler.requested_community_paths)
                for record in platform_catalog["records"]:
                    self.assertTrue(record["local_paths"]["record"].startswith(f"{record['collection']['key']}/"))

                ej_checkpoint = root / "the-economic-journal" / "state" / "checkpoint.json"
                ej_checkpoint_before = ej_checkpoint.read_bytes()
                (root / "restud" / "state" / "checkpoint.json").unlink()
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    main(
                        [
                            "--all-journals",
                            "--resume",
                            "--base-url",
                            f"http://127.0.0.1:{port}",
                            "--output",
                            temp,
                            "--delay",
                            "0",
                            "--retries",
                            "0",
                            "--min-free-gb",
                            "0",
                        ]
                    )
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(ej_checkpoint_before, ej_checkpoint.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class PlatformCatalogTests(unittest.TestCase):
    def test_missing_child_catalog_is_counted_as_a_failure_even_after_zero_exit_code(self):
        profile = resolve_profile("restud")
        args = SimpleNamespace(
            download_files=False,
            max_records=None,
            query="",
            sort="newest",
            page_size=25,
            all_versions=False,
            refresh=False,
            resume=False,
        )
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            destination = write_platform_catalog(Path(temp), (profile,), args, ((profile, 0),))
            catalog = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(0, catalog["summary"]["community_success_count"])
            self.assertEqual(1, catalog["summary"]["community_failure_count"])
            self.assertFalse(catalog["run"]["complete"])


if __name__ == "__main__":
    unittest.main()
