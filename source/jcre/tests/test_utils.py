import unittest

from jcre_scraper.http import UnsafeUrlError, validate_http_url
from jcre_scraper.utils import extract_doi, extract_dois, record_id_from_doi, safe_filename


class UtilityTests(unittest.TestCase):
    def test_doi_normalization(self) -> None:
        self.assertEqual(
            "10.15456/j1.2025190.2321978684",
            extract_doi("https://doi.org/10.15456/J1.2025190.2321978684."),
        )
        self.assertEqual(
            {"10.18718/81781.62", "10.15456/j1.2025190.2321978684"},
            extract_dois(
                "Article 10.18718/81781.62; package https://doi.org/10.15456/J1.2025190.2321978684."
            ),
        )

    def test_record_id(self) -> None:
        self.assertEqual(
            "JCRE_81781_62",
            record_id_from_doi("JCRE", "10.18718/81781.62", "unused"),
        )

    def test_safe_filename(self) -> None:
        self.assertEqual("evil.zip", safe_filename("../../evil.zip"))
        self.assertEqual("download.bin", safe_filename(".."))

    def test_url_allowlist(self) -> None:
        validate_http_url("https://journaldata.zbw.eu/dataset/x", {"journaldata.zbw.eu"})
        with self.assertRaises(UnsafeUrlError):
            validate_http_url("file:///etc/passwd", {"journaldata.zbw.eu"})
        with self.assertRaises(UnsafeUrlError):
            validate_http_url("https://example.org/file.zip", {"journaldata.zbw.eu"})


if __name__ == "__main__":
    unittest.main()
