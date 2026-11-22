import difflib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import default_args
from impact import parse_patch


class Coverage:
    def __init__(self, lines, initializers=(), incomplete=()):
        self.lines = lines
        self.initializers = set(initializers)
        self.incomplete = set(incomplete)

    def line(self, path, number):
        return self.lines.get(number, ())

    def import_initializers(self, path, lines):
        return self.initializers.intersection(lines)


class TestDefaultArguments(unittest.TestCase):
    def select(self, old, new, deps, consumers=""):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = "sqlglot/example.py"
            (base / "sqlglot").mkdir()
            (base / source).write_text(old)
            (base / "tests").mkdir()
            (base / "tests/test_example.py").write_text(consumers)
            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(True),
                    new.splitlines(True),
                    "a/" + source,
                    "b/" + source,
                )
            )
            snapshot = default_args.build(base)
            with patch.object(default_args, "profile", return_value=snapshot):
                return default_args.select(parse_patch(diff)[0], old, base, deps)

    def test_existing_literal_default_selects_all_observed_calls(self):
        old = "def change(a: int = -1, *, mode='on'):\n    return a if mode else 0\n"
        deps = Coverage({2: {"positional", "omitted"}}, incomplete={"unmeasured"})
        self.assertEqual(
            self.select(old, old.replace("-1", "-2"), deps),
            {"positional", "omitted", "unmeasured"},
        )
        self.assertEqual(
            self.select(old, old.replace("'on'", "'off'"), deps),
            {"positional", "omitted", "unmeasured"},
        )

    def test_ambiguous_signatures_and_unobserved_bodies_fall_back(self):
        old = "def decide(flag=True,\n           extra=2):\n    return extra if flag else 0\n"
        for new in [
            old.replace("True", "False, added=None"),
            old.replace("True,", "False #,"),
            old.replace("True", "make_flag()"),
            old.replace("True", "0"),
        ]:
            self.assertIsNone(self.select(old, new, Coverage({3: {"reader"}})))
        self.assertIsNone(self.select(old, old.replace("True", "False"), Coverage({})))
        self.assertIsNone(
            self.select(
                old, old.replace("True", "False"), Coverage({3: {"reader"}}, {3})
            )
        )

    def test_callable_escapes_or_inspection_prevent_narrowing(self):
        old = "def decide(flag=True):\n    return flag\n"
        examples = [
            "from sqlglot.example import decide as callback\n",
            "from sqlglot.example import decide\nmethod = external(decide)\n",
            "def check(callback):\n    return callback.__defaults__\n",
            "from inspect import signature as sig\n",
        ]
        for extra in examples:
            self.assertIsNone(
                self.select(
                    old, old.replace("True", "False"), Coverage({2: {"reader"}}), extra
                )
            )
        for prefix in ["@decorator\n", "def make():\n    "]:
            source = (
                prefix + old
                if prefix[0] == "@"
                else "def make():\n    def decide(flag=True):\n        return flag\n    return decide\n"
            )
            self.assertIsNone(
                self.select(
                    source, source.replace("True", "False"), Coverage({3: {"reader"}})
                )
            )


if __name__ == "__main__":
    unittest.main()
