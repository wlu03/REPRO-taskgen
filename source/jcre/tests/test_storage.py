from pathlib import Path
import tempfile
import unittest

from jcre_scraper.storage import Storage


class StorageTests(unittest.TestCase):
    def test_atomic_json_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "out")
            target = storage.root / "example.json"
            storage.write_json(target, {"answer": 42})
            self.assertEqual({"answer": 42}, storage.read_json(target))

            checkpoint = storage.load_checkpoint()
            checkpoint["records"]["JCRE_81781_62"] = {"status": "inventoried"}
            storage.save_checkpoint(checkpoint)
            loaded = storage.load_checkpoint()
            self.assertEqual("inventoried", loaded["records"]["JCRE_81781_62"]["status"])
            self.assertTrue(loaded["updated_at"].endswith("Z"))

    def test_rejects_relative_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            storage = Storage(Path(temp) / "out")
            with self.assertRaises(ValueError):
                storage.resolve_relative("../../outside.zip")
            self.assertEqual(
                storage.root / "data" / "safe.zip",
                storage.resolve_relative("data/safe.zip"),
            )


if __name__ == "__main__":
    unittest.main()
