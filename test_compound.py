"""Check how a source-only comment participates in a mixed patch."""

import difflib
import unittest
from unittest.mock import Mock, patch

from selector import Selector


def change(path, before, after):
    return "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), "a/" + path, "b/" + path
    ))


class TestCompound(unittest.TestCase):
    def setUp(self):
        self.selector = Selector.__new__(Selector)
        self.selector.all = {"fixture", "raw"}
        self.selector.deps = Mock()
        self.selector.deps.source_readers.return_value = {"raw"}
        self.selector.candidates = Mock(return_value={"fixture"})
        self.fixture = change("tests/fixtures/example.sql", "SELECT 1;\n", "SELECT 2;\n")

    def test_comment_preserves_raw_readers_and_per_row_fixture_selection(self):
        source = "value = 1\n"
        self.selector.source = Mock(return_value=source)
        comment = change("sqlglot/example.py", source, source + "# note\n")
        with patch("verification.refine_many", return_value=set()) as replay:
            self.assertEqual(self.selector.predict(comment + self.fixture), {"raw", "fixture"})
        self.assertEqual({p.path for p in replay.call_args.args[0]}, {"sqlglot/example.py", "tests/fixtures/example.sql"})
        calls = self.selector.candidates.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].kwargs["fixture_batch"])
        self.assertTrue(calls[0].kwargs["allow_probe"])

    def test_hash_line_in_a_multiline_string_is_an_active_edit(self):
        source = 'value = """hello\n# old\n"""\n'
        self.selector.source = Mock(return_value=source)
        code = change("sqlglot/example.py", source, source.replace("# old", "# new"))
        with patch("verification.refine_many", return_value=set()) as replay:
            self.selector.predict(code + self.fixture)
        self.assertEqual(len(replay.call_args.args[0]), 2)
        self.assertEqual(self.selector.candidates.call_count, 2)


if __name__ == "__main__":
    unittest.main()
