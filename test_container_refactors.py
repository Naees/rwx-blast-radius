import difflib
import unittest

from container_refactors import equivalent
from impact import parse_patch


def proven(source, before, after):
    changed = source.replace(before, after)
    patch = parse_patch("".join(difflib.unified_diff(source.splitlines(True), changed.splitlines(True),
                                                     "a/sqlglot/example.py", "b/sqlglot/example.py")))[0]
    return equivalent(patch, source)


class ContainerRefactorsTest(unittest.TestCase):
    def test_truth_and_reversal_preserve_semantics_for_exact_locals(self):
        src = "def f():\n    left, xs = None, []\n    xs = [left] + xs\n    if xs:\n        xs.reverse()\n"
        self.assertTrue(proven(src, "if xs:", "if len(xs) > 0:"))
        self.assertTrue(proven(src, "xs.reverse()", "xs[:] = reversed(xs)"))
        self.assertFalse(proven(src, "xs.reverse()", "xs[1:] = reversed(xs)"))
        self.assertFalse(proven(src, "if xs:", "if len(xs) == 0:"))
        self.assertFalse(proven(src, "if xs:", "if len(xs) > 0 and left:"))

    def test_container_type_and_builtin_bindings_must_be_proven(self):
        base = "def f(input):\n    xs = []\n    if xs:\n        return 1\n"
        for alt in ("xs = input", "xs = []\n    xs = input", "xs = []\n    def g():\n        nonlocal xs\n        xs = input",
                    "xs = []\n    len = input", "xs = []\n    exec(input)", "xs = []\n    for xs in input:\n        pass"):
            with self.subTest(alt=alt):
                self.assertFalse(proven(base.replace("xs = []", alt), "if xs:", "if len(xs):"))
        self.assertFalse(proven("from somewhere import len\n" + base, "if xs:", "if len(xs):"))
        self.assertFalse(proven(base.replace("xs = []", "xs = ()").replace("return 1", "xs.reverse()"),
                                "xs.reverse()", "xs[:] = reversed(xs)"))

    def test_scopes_and_other_changes_stay_distinct(self):
        src = "def f():\n    xs = {}\n    def g(xs):\n        if xs:\n            return 1\n"
        self.assertFalse(proven(src, "if xs:", "if bool(xs):"))
        src = "def f():\n    xs = []\n    if not xs:\n        return 1\n"
        self.assertTrue(proven(src, "if not xs:", "if len(xs) == 0:"))
        self.assertFalse(proven(src, "if not xs:\n        return 1", "if len(xs) == 0:\n        return 2"))
