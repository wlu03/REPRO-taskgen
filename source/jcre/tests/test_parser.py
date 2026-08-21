from pathlib import Path
import unittest

from jcre_scraper.parser import parse_publications


FIXTURES = Path(__file__).parent / "fixtures"


class PublicationsParserTests(unittest.TestCase):
    def test_parses_publications_and_package_links(self) -> None:
        html = (FIXTURES / "publications.html").read_text(encoding="utf-8")
        output = parse_publications(html, "https://jcr-econ.org/publications/")

        self.assertEqual([], output.warnings)
        self.assertEqual(3, len(output.records))

        first = output.records[0].record
        self.assertEqual("JCRE_81781_62", first.record_id)
        self.assertEqual("JCRE", first.journal_code)
        self.assertEqual(5, first.volume)
        self.assertEqual(2026, first.year)
        self.assertEqual("2026-11", first.issue)
        self.assertEqual("10.18718/81781.62", first.article_doi)
        self.assertEqual("10.15456/j1.2025190.2321978684", first.replication.doi)
        self.assertEqual(
            "https://doi.org/10.15456/j1.2025190.2321978684",
            first.replication.link_url,
        )
        self.assertIn("R code", first.replication.availability_text)
        self.assertEqual(1, len(first.related_links))
        self.assertEqual("https://example.org/note", first.related_links[0].url)

        reply = output.records[1].record
        self.assertEqual("no_link", reply.replication.inventory_status)
        self.assertIsNone(reply.replication.link_url)

        old = output.records[2].record
        self.assertEqual("IREE", old.journal_code)
        self.assertEqual("2018-3", old.issue)
        self.assertEqual(
            "https://www.journaldata.zbw.eu/dataset/data-set-for-roodman-replication-of-bleakley-2007",
            old.replication.link_url,
        )
        self.assertIsNone(old.replication.doi)
        self.assertIn("Stata code", old.replication.availability_text)


if __name__ == "__main__":
    unittest.main()
