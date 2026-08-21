from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from zenodo_community_harvester.cli import _reconcile_local_download_state, main


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureHandler(BaseHTTPRequestHandler):
    routes = {
        "/api/communities/ej-replication-repository": "community.json",
        "/api/communities/ej-replication-repository/records": "search_page.json",
        "/api/records/21725672": "record.json",
        "/api/records/21725672/files": "files.json",
    }

    def do_GET(self):
        path = urlsplit(self.path).path
        expected_accept = "application/json" if path.endswith("/files") else "application/vnd.inveniordm.v1+json"
        if self.headers.get("Accept") != expected_accept:
            self.send_error(406)
            return
        fixture_name = self.routes.get(path)
        if not fixture_name:
            self.send_error(404)
            return
        payload = (FIXTURES / fixture_name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class CLIIntegrationTests(unittest.TestCase):
    def test_inventory_end_to_end_against_loopback_fixture_api(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
                port = server.server_address[1]
                code = main(
                    [
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
                catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
                record = json.loads((root / "data" / "21725672" / "record.json").read_text(encoding="utf-8"))
                self.assertEqual(1, catalog["summary"]["record_count"])
                self.assertEqual(2, catalog["summary"]["hosted_file_count"])
                self.assertEqual(record, catalog["records"][0])
                self.assertEqual(
                    (FIXTURES / "record.json").read_bytes(),
                    (root / "data" / "21725672" / "api_response.json").read_bytes(),
                )
                self.assertEqual(
                    (FIXTURES / "files.json").read_bytes(),
                    (root / "data" / "21725672" / "files_response.json").read_bytes(),
                )
                checkpoint_path = root / "state" / "checkpoint.json"
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                checkpoint["schema_version"] = "1.0.0"
                checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
                resume_code = main(
                    [
                        "--inventory-only",
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
                self.assertEqual(0, resume_code)
                self.assertEqual("2.0.0", json.loads(checkpoint_path.read_text(encoding="utf-8"))["schema_version"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_resume_requires_existing_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--resume", "--output", temp, "--delay", "0"])
            self.assertEqual(2, raised.exception.code)

    def test_output_scope_mismatch_is_rejected_before_cache_reuse(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            checkpoint = Path(temp) / "state" / "checkpoint.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(json.dumps({"config_fingerprint": "sha256:different"}), encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                main(["--inventory-only", "--output", temp, "--delay", "0"])
            self.assertEqual(2, raised.exception.code)

    def test_lookalike_base_origin_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--base-url", "https://zenodo.org.evil.example", "--output", temp])
            self.assertEqual(2, raised.exception.code)

    def test_token_is_never_sent_to_loopback_test_origin(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(
            "os.environ", {"ZENODO_TOKEN": "secret-test-token"}
        ):
            with self.assertRaises(SystemExit) as raised:
                main(["--base-url", "http://127.0.0.1:9999", "--output", temp])
            self.assertEqual(2, raised.exception.code)

    def test_missing_local_file_invalidates_downloaded_catalog_state(self):
        payload = b"verified bytes"
        checksum = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        record = {
            "hosted_files": [
                {
                    "status": "downloaded",
                    "local_path": "data/1/files/f/file.bin",
                    "size_bytes": len(payload),
                    "checksum": f"md5:{checksum}",
                    "downloaded_bytes": len(payload),
                    "downloaded_at": "2026-08-21T00:00:00Z",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp:
            reconciled = _reconcile_local_download_state(record, Path(temp))
            self.assertEqual("not_requested", reconciled["hosted_files"][0]["status"])
            destination = Path(temp) / "data" / "1" / "files" / "f" / "file.bin"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(payload)
            record["hosted_files"][0]["status"] = "downloaded"
            reconciled = _reconcile_local_download_state(record, Path(temp))
            self.assertEqual("downloaded", reconciled["hosted_files"][0]["status"])


if __name__ == "__main__":
    unittest.main()
