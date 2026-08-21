import json
import tempfile
import unittest
from pathlib import Path

from scraper import HttpClient, ScraperError, redact_sensitive_urls


class HttpCacheTests(unittest.TestCase):
    def setUp(self):
        self.http = HttpClient(
            timeout=1,
            retries=0,
            delay=0,
            user_agent="offline-test",
        )

    def tearDown(self):
        self.http.close()

    def test_cache_only_reads_existing_bytes_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "response.json"
            path.write_text('{"ok": true}', encoding="utf-8")
            value, source = self.http.fetch_json(
                "https://invalid.example/never-requested",
                path,
                prefer_cache=True,
                cache_only=True,
            )
            self.assertEqual(value, {"ok": True})
            self.assertEqual(source["cache"], "hit")

    def test_cache_only_fails_closed_when_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "missing.json"
            with self.assertRaises(ScraperError):
                self.http.fetch_json(
                    "https://invalid.example/never-requested",
                    path,
                    prefer_cache=True,
                    cache_only=True,
                )

    def test_signed_query_credentials_are_redacted_from_errors(self):
        message = (
            "403 for https://objects.example/file.pdf?"
            "X-Amz-Credential=secret&X-Amz-Signature=also-secret"
        )
        redacted = redact_sensitive_urls(message)
        self.assertEqual(redacted, "403 for https://objects.example/file.pdf")
        self.assertNotIn("secret", redacted)


if __name__ == "__main__":
    unittest.main()
