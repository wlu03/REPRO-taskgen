import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scraper import (
    ArtifactSkipped,
    Downloader,
    ScraperError,
    build_summary,
    merge_previous_artifact_state,
)


class FakeResponse:
    def __init__(self, body, status=200, headers=None, url="https://files.example/data.bin"):
        self.body = body
        self.status_code = status
        self.headers = dict(headers or {})
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        yield self.body

    def close(self):
        pass


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = []

    def get(self, url, **kwargs):
        self.headers.append(dict(kwargs.get("headers") or {}))
        return self.responses.pop(0)


def artifact():
    return {
        "artifact_id": "abc123",
        "role": "repository_file",
        "provider": "test",
        "source_url": "https://files.example/data.bin",
        "filename": "data.bin",
        "size_bytes": None,
        "sha256": None,
        "status": "discovered",
        "local_path": None,
    }


def write_resume_state(target_dir, item, *, etag='"version-1"', total=6):
    state = {
        "schema_version": "1.0",
        "download_url": item["source_url"],
        "source_fingerprint": Downloader._source_fingerprint(item),
        "etag": etag,
        "last_modified": None,
        "expected_total": total,
        "content_encoding": None,
    }
    (target_dir / ".download.part.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


class DownloaderTests(unittest.TestCase):
    def test_download_streams_atomically_and_hashes(self):
        body = b"reproducible bytes"
        response = FakeResponse(
            body,
            headers={
                "Content-Length": str(len(body)),
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="result.dat"',
            },
        )
        http = FakeHttp([response])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            item = artifact()
            downloader = Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=False,
            )
            downloader.download(item, root / "artifact")
            self.assertEqual((root / item["local_path"]).read_bytes(), body)
            self.assertEqual(item["filename"], "result.dat")
            self.assertEqual(item["sha256"], hashlib.sha256(body).hexdigest())
            self.assertFalse((root / "artifact" / ".download.part").exists())

    def test_upstream_reserved_partial_names_are_remapped(self):
        for upstream_name in (".download.part", ".download.part.json"):
            with self.subTest(upstream_name=upstream_name):
                response = FakeResponse(
                    b"payload",
                    headers={
                        "Content-Length": "7",
                        "Content-Disposition": (
                            f'attachment; filename="{upstream_name}"'
                        ),
                    },
                )
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary).resolve()
                    item = artifact()
                    Downloader(
                        FakeHttp([response]),
                        output_root=root,
                        max_file_bytes=100,
                        min_free_bytes=0,
                        resume=False,
                    ).download(item, root / "artifact")
                    saved = root / item["local_path"]
                    self.assertEqual(saved.read_bytes(), b"payload")
                    self.assertNotIn(saved.name.casefold(), {
                        ".download.part",
                        ".download.part.json",
                    })
                    self.assertFalse((root / "artifact" / ".download.part").exists())
                    self.assertFalse(
                        (root / "artifact" / ".download.part.json").exists()
                    )

    def test_resume_requires_valid_content_range(self):
        response = FakeResponse(
            b"def",
            status=206,
            headers={
                "Content-Range": "bytes 3-5/6",
                "Content-Length": "3",
                "ETag": '"version-1"',
            },
        )
        http = FakeHttp([response])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            (target_dir / ".download.part").write_bytes(b"abc")
            item = artifact()
            write_resume_state(target_dir, item)
            downloader = Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=True,
            )
            downloader.download(item, target_dir)
            self.assertEqual((root / item["local_path"]).read_bytes(), b"abcdef")
            self.assertEqual(http.headers[0]["Range"], "bytes=3-")
            self.assertEqual(http.headers[0]["If-Range"], '"version-1"')
            self.assertEqual(http.headers[0]["Accept-Encoding"], "identity")
            self.assertFalse((target_dir / ".download.part.json").exists())

    def test_invalid_range_response_restarts(self):
        ranged_but_ignored = FakeResponse(b"abcdef", status=200)
        fresh = FakeResponse(b"abcdef", status=200)
        http = FakeHttp([ranged_but_ignored, fresh])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            (target_dir / ".download.part").write_bytes(b"abc")
            item = artifact()
            write_resume_state(target_dir, item)
            Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=True,
            ).download(item, target_dir)
            self.assertEqual((root / item["local_path"]).read_bytes(), b"abcdef")
            self.assertNotIn("Range", http.headers[1])

    def test_incomplete_valid_range_is_not_marked_complete(self):
        response = FakeResponse(
            b"def",
            status=206,
            headers={
                "Content-Range": "bytes 3-5/100",
                "Content-Length": "3",
                "ETag": '"version-1"',
            },
        )
        http = FakeHttp([response])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            (target_dir / ".download.part").write_bytes(b"abc")
            item = artifact()
            write_resume_state(target_dir, item, total=100)
            downloader = Downloader(
                http,
                output_root=root,
                max_file_bytes=200,
                min_free_bytes=0,
                resume=True,
            )
            with self.assertRaises(ScraperError):
                downloader.download(item, target_dir)
            self.assertEqual((target_dir / ".download.part").read_bytes(), b"abcdef")
            self.assertEqual(item["status"], "discovered")

    def test_resume_without_validator_sidecar_restarts_from_zero(self):
        http = FakeHttp([FakeResponse(b"fresh", headers={"Content-Length": "5"})])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            (target_dir / ".download.part").write_bytes(b"stale")
            item = artifact()
            Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=True,
            ).download(item, target_dir)
            self.assertEqual((root / item["local_path"]).read_bytes(), b"fresh")
            self.assertNotIn("Range", http.headers[0])

    def test_changed_etag_never_appends_to_stale_partial(self):
        changed_range = FakeResponse(
            b"NEW",
            status=206,
            headers={
                "Content-Range": "bytes 3-5/6",
                "Content-Length": "3",
                "ETag": '"version-2"',
            },
        )
        fresh = FakeResponse(
            b"abcdef",
            headers={"Content-Length": "6", "ETag": '"version-2"'},
        )
        http = FakeHttp([changed_range, fresh])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            (target_dir / ".download.part").write_bytes(b"OLD")
            item = artifact()
            write_resume_state(target_dir, item, etag='"version-1"')
            Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=True,
            ).download(item, target_dir)
            self.assertEqual((root / item["local_path"]).read_bytes(), b"abcdef")
            self.assertNotIn("Range", http.headers[1])

    def test_size_guard_stops_before_writing_known_large_file(self):
        http = FakeHttp([FakeResponse(b"x" * 10, headers={"Content-Length": "10"})])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            item = artifact()
            downloader = Downloader(
                http,
                output_root=root,
                max_file_bytes=5,
                min_free_bytes=0,
                resume=False,
            )
            with self.assertRaises(ArtifactSkipped):
                downloader.download(item, root / "artifact")
            self.assertEqual(item["status"], "skipped_size_limit")

    def test_declared_size_guard_makes_no_request(self):
        http = FakeHttp([])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            item = artifact()
            item["size_bytes"] = 10
            downloader = Downloader(
                http,
                output_root=root,
                max_file_bytes=5,
                min_free_bytes=0,
                resume=False,
            )
            with self.assertRaises(ArtifactSkipped):
                downloader.download(item, root / "artifact")
            self.assertEqual(http.headers, [])

    def test_refreshed_source_checksum_forces_replacement(self):
        old = b"old"
        new = b"new"
        response = FakeResponse(new, headers={"Content-Length": "3"})
        http = FakeHttp([response])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            old_path = target_dir / "data.bin"
            old_path.write_bytes(old)
            fresh = artifact()
            fresh.update(
                {
                    "size_bytes": 3,
                    "source_checksum": "md5:" + hashlib.md5(new).hexdigest(),
                    "source_modified_at": "2026-01-02T00:00:00Z",
                }
            )
            previous = {
                "artifacts": [
                    {
                        **artifact(),
                        "size_bytes": 3,
                        "source_checksum": "md5:" + hashlib.md5(old).hexdigest(),
                        "source_modified_at": "2026-01-01T00:00:00Z",
                        "sha256": hashlib.sha256(old).hexdigest(),
                        "status": "downloaded",
                        "local_path": str(old_path.relative_to(root)),
                    }
                ]
            }
            merge_previous_artifact_state([fresh], previous)
            self.assertTrue(fresh["source_changed_since_previous"])
            Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=False,
            ).download(fresh, target_dir)
            self.assertEqual((root / fresh["local_path"]).read_bytes(), new)

    def test_bad_refresh_never_overwrites_prior_complete_file(self):
        prior = b"prior-good-version"
        expected_new = b"expected-new-version"
        bad_response = b"corrupt-new-version"
        response = FakeResponse(
            bad_response,
            headers={"Content-Length": str(len(bad_response))},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            target = target_dir / "data.bin"
            target.write_bytes(prior)
            item = artifact()
            item.update(
                {
                    "source_changed_since_previous": True,
                    "source_checksum": (
                        "sha256:" + hashlib.sha256(expected_new).hexdigest()
                    ),
                }
            )
            downloader = Downloader(
                FakeHttp([response]),
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=False,
            )
            with self.assertRaisesRegex(ScraperError, "checksum mismatch"):
                downloader.download(item, target_dir)
            self.assertEqual(target.read_bytes(), prior)
            self.assertEqual(
                (target_dir / ".download.part").read_bytes(), bad_response
            )

    def test_pending_source_change_survives_failed_run_until_replaced(self):
        old = b"old"
        new = b"new"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target_dir = root / "artifact"
            target_dir.mkdir()
            old_path = target_dir / "data.bin"
            old_path.write_bytes(old)
            prior = {
                **artifact(),
                "size_bytes": 3,
                "source_modified_at": "2026-01-01T00:00:00Z",
                "sha256": hashlib.sha256(old).hexdigest(),
                "status": "downloaded",
                "local_path": str(old_path.relative_to(root)),
            }
            failed_refresh = {
                **artifact(),
                "size_bytes": 3,
                "source_modified_at": "2026-01-02T00:00:00Z",
            }
            merge_previous_artifact_state(
                [failed_refresh], {"artifacts": [prior]}
            )
            self.assertTrue(failed_refresh["source_changed_since_previous"])
            failed_refresh["status"] = "error"

            retry = {
                **artifact(),
                "size_bytes": 3,
                "source_modified_at": "2026-01-02T00:00:00Z",
            }
            merge_previous_artifact_state(
                [retry], {"artifacts": [failed_refresh]}
            )
            self.assertTrue(retry["source_changed_since_previous"])
            http = FakeHttp(
                [FakeResponse(new, headers={"Content-Length": str(len(new))})]
            )
            Downloader(
                http,
                output_root=root,
                max_file_bytes=100,
                min_free_bytes=0,
                resume=False,
            ).download(retry, target_dir)
            self.assertEqual((root / retry["local_path"]).read_bytes(), new)
            self.assertEqual(len(http.headers), 1)
            self.assertNotIn("source_changed_since_previous", retry)

    def test_summary_counts_artifacts(self):
        item = artifact()
        item.update({"status": "downloaded", "size_bytes": 20})
        record = {
            "paper": {"reference_url": "https://doi.org/10/example"},
            "repository": {"provider": "github", "locator": "org/repo"},
            "check": {"type": "journal", "venue": "Example"},
            "artifacts": [item],
            "errors": [],
        }
        summary = build_summary([record])
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["repository_artifacts"], 1)
        self.assertEqual(summary["known_artifact_bytes"], 20)


if __name__ == "__main__":
    unittest.main()
