from __future__ import annotations

import hashlib
import http.client
import io
import tempfile
import unittest
from pathlib import Path

from zenodo.http import DownloadSkipped, HTTPClient, download_file, validate_remote_url
from zenodo.util import file_matches, parse_checksum


class URLSafetyTests(unittest.TestCase):
    def test_accepts_zenodo_https(self):
        value = "https://zenodo.org/api/records/1/files/a/content"
        self.assertEqual(value, validate_remote_url(value, "https://zenodo.org"))

    def test_accepts_zenodo_subdomain(self):
        value = "https://sandbox.zenodo.org/api/records/1"
        self.assertEqual(value, validate_remote_url(value, "https://zenodo.org"))

    def test_rejects_lookalike_and_non_https(self):
        for value in (
            "https://zenodo.org.evil.example/file",
            "http://zenodo.org/file",
            "file:///etc/passwd",
            "https://user:pass@zenodo.org/file",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_remote_url(value, "https://zenodo.org")


class IntegrityTests(unittest.TestCase):
    def test_checksum_parsing_and_matching(self):
        payload = b"fixture bytes"
        checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
        self.assertEqual("md5", parse_checksum(checksum)[0])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file.bin"
            path.write_bytes(payload)
            self.assertTrue(file_matches(path, len(payload), checksum))
            self.assertFalse(file_matches(path, len(payload) + 1, checksum))


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, status: int = 200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status

    def geturl(self):
        return "https://zenodo.org/file"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class BrokenReadResponse(FakeResponse):
    def read(self, size=-1):
        raise http.client.IncompleteRead(b"{}", 100)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = []
        self.accepts = []

    def open(self, url, *, accept, headers=None):
        self.accepts.append(accept)
        self.headers.append(headers or {})
        return self.responses.pop(0)


class DownloadTests(unittest.TestCase):
    @staticmethod
    def checksum(payload: bytes) -> str:
        return f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"

    def test_streams_to_part_verifies_and_renames(self):
        payload = b"complete download"
        client = FakeClient([FakeResponse(payload, headers={"Content-Length": str(len(payload))})])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "data" / "record" / "file.bin"
            result = download_file(
                client,
                url="https://zenodo.org/file",
                destination=destination,
                output_root=root,
                expected_size=len(payload),
                checksum=self.checksum(payload),
                resume=False,
                max_bytes=None,
                min_free_bytes=0,
            )
            self.assertEqual("downloaded", result.status)
            self.assertEqual(["*/*"], client.accepts)
            self.assertEqual(payload, destination.read_bytes())
            self.assertFalse(destination.with_name("file.bin.part").exists())

    def test_resume_appends_only_from_matching_range(self):
        complete = b"abcdefghij"
        partial = complete[:4]
        response = FakeResponse(
            complete[4:],
            status=206,
            headers={"Content-Range": "bytes 4-9/10", "Content-Length": "6"},
        )
        client = FakeClient([response])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "file.bin"
            destination.with_name("file.bin.part").write_bytes(partial)
            download_file(
                client,
                url="https://zenodo.org/file",
                destination=destination,
                output_root=root,
                expected_size=len(complete),
                checksum=self.checksum(complete),
                resume=True,
                max_bytes=None,
                min_free_bytes=0,
            )
            self.assertEqual("bytes=4-", client.headers[0]["Range"])
            self.assertEqual(complete, destination.read_bytes())

    def test_known_oversized_file_is_skipped_before_request(self):
        client = FakeClient([])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(DownloadSkipped):
                download_file(
                    client,
                    url="https://zenodo.org/file",
                    destination=root / "file.bin",
                    output_root=root,
                    expected_size=11,
                    checksum="",
                    resume=False,
                    max_bytes=10,
                    min_free_bytes=0,
                )
            self.assertEqual([], client.headers)


class MetadataReadTests(unittest.TestCase):
    def test_truncated_json_body_is_retried_from_scratch(self):
        client = HTTPClient(
            base_url="https://zenodo.org",
            user_agent="test",
            delay=0,
            timeout=1,
            retries=1,
            sleep=lambda _: None,
        )
        responses = [BrokenReadResponse(b"{}"), FakeResponse(b'{"ok": true}')]
        client.open = lambda url, accept: responses.pop(0)
        payload, _, status, _ = client.get_bytes("https://zenodo.org/api/records/1")
        self.assertEqual(b'{"ok": true}', payload)
        self.assertEqual(200, status)
        self.assertEqual([], responses)


if __name__ == "__main__":
    unittest.main()
