import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from jcre_scraper.ckan import CkanResolution, normalize_package_result
from jcre_scraper.scraper import JcreScraper, ScrapeConfig


FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttp:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls = []

    def get_text(self, url, *, allowed_hosts):
        self.calls.append({"url": url, "allowed_hosts": set(allowed_hosts)})
        response = SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "text/html; charset=UTF-8", "ETag": "fixture"},
        )
        opened = SimpleNamespace(response=response, final_url=url, redirect_chain=[])
        return self.html, opened


class ScraperIntegrationTests(unittest.TestCase):
    def test_source_cache_is_scoped_to_requested_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            http = FakeHttp("<html>new source</html>")
            config = ScrapeConfig(
                output_dir=output,
                source_url="https://mirror.example/publications/",
            )
            scraper = JcreScraper(config, http)
            scraper.storage.write_text(scraper.storage.source_dir / "publications.html", "<html>stale</html>")
            scraper.storage.write_json(
                scraper.storage.source_dir / "publications_response.json",
                {"requested_url": "https://jcr-econ.org/publications/"},
            )

            self.assertEqual("<html>new source</html>", scraper._fetch_publications())
            self.assertEqual(1, len(http.calls))
            self.assertIn("mirror.example", http.calls[0]["allowed_hosts"])

            self.assertEqual("<html>new source</html>", scraper._fetch_publications())
            self.assertEqual(1, len(http.calls))

    def test_offline_end_to_end_inventory(self) -> None:
        html = (FIXTURES / "publications.html").read_text(encoding="utf-8")
        package_payload = json.loads((FIXTURES / "package_show.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp:
            config = ScrapeConfig(output_dir=Path(temp) / "out", max_records=2)
            scraper = JcreScraper(config, FakeHttp(html))

            def resolve(package):
                normalized = normalize_package_result(
                    package_payload["result"],
                    availability_text=package.availability_text,
                    link_url=package.link_url,
                    doi=package.doi,
                    resolved_url=(
                        "https://journaldata.zbw.eu/dataset/"
                        "replication-package-reexamining-the-effect-of-clean-water"
                    ),
                )
                return CkanResolution(
                    package=normalized,
                    landing_metadata={"resolved_url": normalized.resolved_url, "redirect_chain": []},
                    landing_html=b"<html><body>fixture</body></html>",
                    package_response=package_payload,
                )

            scraper.journaldata.resolve = resolve
            catalog = scraper.run()

            self.assertEqual(2, catalog["summary"]["record_count"])
            self.assertEqual(1, catalog["summary"]["records_with_replication_link"])
            self.assertEqual(3, catalog["summary"]["resource_count"])
            self.assertEqual(123456, catalog["summary"]["estimated_download_bytes_from_known_sizes"])
            self.assertTrue((config.output_dir / "catalog.json").exists())
            first_dir = config.output_dir / "data" / "JCRE_81781_62"
            self.assertTrue((first_dir / "record.json").exists())
            self.assertTrue((first_dir / "publication_fragment.html").exists())
            self.assertTrue((first_dir / "ckan_response.json").exists())
            record = json.loads((first_dir / "record.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["records"][0], record)


if __name__ == "__main__":
    unittest.main()
