"""A helper can affect both its runtime callers and imports that already ran."""

import difflib
import unittest
from enum import Enum, auto
from pathlib import Path
from unittest.mock import Mock

from impact import parse_patch
from selector import Selector


class TestImportEffects(unittest.TestCase):
    def test_enum_initializer_and_ordinary_caller_both_need_selection(self):
        source = (
            "class AutoName(Enum):\n"
            "    def _generate_next_value_(name, _start, _count, _last_values):\n"
            "        return name\n"
        )
        updated = source.replace("return name", "return name[9999]")
        consumer = "class NormalizationStrategy(AutoName):\n    UPPERCASE = auto()\n"
        namespace = {"Enum": Enum, "auto": auto}
        exec(source + consumer, namespace)
        self.assertEqual(
            namespace["NormalizationStrategy"].UPPERCASE.value, "UPPERCASE"
        )
        with self.assertRaises(IndexError):
            exec(updated + consumer, namespace)

        path = "sqlglot/helper.py"
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(True),
                updated.splitlines(True),
                "a/" + path,
                "b/" + path,
            )
        )
        selector = Selector.__new__(Selector)
        selector.base = Path(".")
        selector.source = Mock(return_value=source)
        selector.fixtures = Mock()
        selector.fixtures.select.return_value = None
        selector.all = {"direct caller", "imports dialect", "unrelated"}
        selector.deps = Mock()
        selector.deps.tests = selector.all
        selector.deps.line.return_value = {"direct caller"}
        selector.deps.source_readers.return_value = set()
        selector.deps.import_initializers.return_value = {"sqlglot/dialects/dialect.py"}
        selector.import_users = Mock(return_value={"imports dialect"})
        self.assertEqual(
            selector.candidates(parse_patch(diff)[0]),
            {"direct caller", "imports dialect"},
        )
        selector.import_users.assert_called_once_with("sqlglot/dialects/dialect.py")


if __name__ == "__main__":
    unittest.main()
