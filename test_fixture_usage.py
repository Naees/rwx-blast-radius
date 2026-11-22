"""Regression checks for setup failures and per-row field ownership."""

import difflib
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fixture_usage
from impact import parse_patch


class TestFixtureUsage(unittest.TestCase):
    path = "tests/fixtures/example.sql"
    source = "SELECT 1;\n1;\nSELECT 2;\n2;\n"
    ids = ["first", "second", "setup", "raw"]

    def select(self, updated):
        before = self.source
        file = {
            "sha256": hashlib.sha256(before.encode()).hexdigest(),
            "rows": [{"expected:read": 1}, {"expected:read": 2, "meta:flag": 2}],
            "raw": 3,
        }
        data = {
            "files": {self.path: file},
            "pool": ["0", "1", "2", "8"],
            "tests": self.ids,
            "unknown": [],
        }
        change = "".join(
            difflib.unified_diff(
                before.splitlines(True),
                updated.splitlines(True),
                "a/" + self.path,
                "b/" + self.path,
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            fixture = base / self.path
            fixture.parent.mkdir(parents=True)
            fixture.write_text(before)
            deps = Mock()
            deps.fixture.return_value = set(self.ids)
            with patch.object(fixture_usage, "_profile", return_value=data):
                return fixture_usage.select(
                    parse_patch(change)[0], base, self.ids, deps
                )

    def test_changed_expected_and_previously_absent_metadata_key(self):
        self.assertEqual(
            self.select(self.source.replace("\n1;", "\n9;")), {"first", "raw"}
        )
        self.assertEqual(
            self.select(self.source.replace("SELECT 2", "# flag: yes\nSELECT 2")),
            {"second", "raw"},
        )

    def test_raw_reader_sees_loader_ignored_comments(self):
        self.assertEqual(self.select("-- comment\n" + self.source), {"raw"})

    def test_unparseable_metadata_and_row_count_change_keep_setup_fallback(self):
        self.assertIsNone(self.select("# missing colon\n" + self.source))
        self.assertIsNone(self.select(self.source + "SELECT 3;\n3;\n"))


if __name__ == "__main__":
    unittest.main()
