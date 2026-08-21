from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from jcre_scraper.http import HttpClient, OpenedResponse


class FakeResponse:
    def __init__(self, *, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class QueuedHttpClient(HttpClient):
    def __init__(self, opened: list[OpenedResponse]) -> None:
        self.opened = opened

    def open(self, *args, **kwargs):
        return self.opened.pop(0)


class DownloadResumeTests(unittest.TestCase):
    def test_416_marks_complete_partial_as_resumed(self) -> None:
        response = FakeResponse(status_code=416, headers={"Content-Range": "bytes */3"})
        opened = OpenedResponse(
            response=response,
            final_url="https://journaldata.zbw.eu/resource/file.zip",
            redirect_chain=[],
        )
        client = QueuedHttpClient([opened])

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "file.zip"
            partial = Path(temp) / "file.zip.part"
            partial.write_bytes(b"abc")

            result = client.download_to(
                "https://journaldata.zbw.eu/resource/file.zip",
                target,
                allowed_hosts={"journaldata.zbw.eu"},
                resume=True,
                max_bytes=None,
                min_free_bytes=0,
            )

            self.assertEqual("resumed", result.status)
            self.assertEqual(b"abc", target.read_bytes())
            self.assertFalse(partial.exists())
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
