import json
from pathlib import Path
import unittest

from jcre_scraper.ckan import dataset_slug_from_url, normalize_package_result


FIXTURES = Path(__file__).parent / "fixtures"


class CkanTests(unittest.TestCase):
    def test_extracts_dataset_slug(self) -> None:
        self.assertEqual(
            "replication-package-test",
            dataset_slug_from_url("https://journaldata.zbw.eu/dataset/replication-package-test?x=1"),
        )
        self.assertIsNone(dataset_slug_from_url("https://journaldata.zbw.eu/info/about"))

    def test_normalizes_package_and_resources(self) -> None:
        payload = json.loads((FIXTURES / "package_show.json").read_text(encoding="utf-8"))
        package = normalize_package_result(
            payload["result"],
            availability_text="The R code and data are available.",
            link_url="https://doi.org/10.15456/j1.2025190.2321978684",
            doi="10.15456/j1.2025190.2321978684",
            resolved_url="https://journaldata.zbw.eu/dataset/replication-package-reexamining-the-effect-of-clean-water",
        )

        self.assertEqual("complete", package.inventory_status)
        self.assertEqual("cc-by-4.0", package.license_id)
        self.assertEqual(["replication", "R"], package.tags)
        self.assertEqual(3, len(package.resources))
        self.assertTrue(package.resources[0].downloadable)
        self.assertEqual(123456, package.resources[0].size_bytes)
        self.assertFalse(package.resources[1].hosted_by_repository)
        self.assertFalse(package.resources[1].downloadable)
        self.assertTrue(package.resources[2].hosted_by_repository)
        self.assertFalse(package.resources[2].downloadable)


if __name__ == "__main__":
    unittest.main()
