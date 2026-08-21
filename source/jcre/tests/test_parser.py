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


class MultiPublicationBlockTests(unittest.TestCase):
    """The publications page is hand-maintained HTML.

    One block on the live page packs four consecutive publications into a
    single <p>, separated by <br/> rather than by separate paragraphs. The
    parser used to emit only the first DOI in such a block and drop the rest
    without warning, so the loss was invisible in a successful run.
    """

    MERGED_DOIS = [
        "10.18718/81781.41",
        "10.18718/81781.42",
        "10.18718/81781.43",
        "10.18718/81781.44",
    ]

    def _parse_fixture(self):
        html = (FIXTURES / "publications_multi_doi_block.html").read_text(encoding="utf-8")
        return parse_publications(html, "https://jcr-econ.org/publications/")

    def test_every_publication_in_a_merged_block_becomes_a_record(self) -> None:
        output = self._parse_fixture()

        self.assertEqual([], output.warnings)
        self.assertEqual(
            self.MERGED_DOIS,
            sorted(parsed.record.article_doi for parsed in output.records),
        )

    def test_merged_publications_keep_their_own_metadata(self) -> None:
        output = self._parse_fixture()
        by_doi = {parsed.record.article_doi: parsed.record for parsed in output.records}

        # Each entry keeps its own issue, title, and replication package rather
        # than inheriting them from whichever publication came first.
        self.assertEqual(
            ["2025-2", "2025-3", "2025-4", "2025-5"],
            sorted(record.issue for record in by_doi.values()),
        )
        titles = {record.title for record in by_doi.values()}
        self.assertEqual(4, len(titles))
        links = {record.replication.link_url for record in by_doi.values()}
        self.assertEqual(4, len(links))

        # The volume heading precedes the merged block, so it must still resolve
        # for every publication split out of it.
        for record in by_doi.values():
            self.assertEqual("JCRE", record.journal_code)
            self.assertEqual(4, record.volume)
            self.assertEqual(2025, record.year)

    def test_unreachable_doi_is_reported_instead_of_dropped(self) -> None:
        # A DOI that reaches no record must surface as a warning, so a future
        # page shape the splitter cannot handle fails loudly rather than silently.
        html = (
            '<div class="entry-content">'
            "<h3>JCRE, Volume 4, 2025</h3>"
            '<p><a href="https://doi.org/10.18718/81781.99">10.18718/81781.99</a>'
            "<em>Journal of Comments and Replications in Economics, Vol.4, 2025-9</em>."
            "</p>"
            "<table><tr><td>10.18718/81781.98</td></tr></table>"
            "</div>"
        )
        output = parse_publications(html, "https://jcr-econ.org/publications/")

        self.assertEqual(1, len(output.records))
        self.assertTrue(
            any("10.18718/81781.98" in warning for warning in output.warnings),
            f"expected the unreachable DOI to be reported, got {output.warnings}",
        )
