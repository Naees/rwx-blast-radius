import difflib
import unittest

from container_refactors import equivalent
from impact import parse_patch
from unittest.mock import patch as replace


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

    def test_short_circuit_truth_only_and_shadowed_module_names(self):
        src = "def f(ready):\n    xs = []\n    if ready and xs:\n        return ready or xs\n"
        self.assertTrue(proven(src, "ready and xs:", "ready and len(xs):"))
        self.assertFalse(proven(src, "return ready or xs", "return ready or len(xs)"))
        self.assertFalse(proven(src, "if ready and xs:", "if len(xs) and ready:"))
        # A cached scope window omits surrounding imports. They still determine
        # which callable the bare name resolves to inside this function.
        shadowed = "from example import len\n" + src
        with replace("container_refactors.source_window",
                     return_value=(src, src.replace("ready and xs:", "ready and len(xs):"))):
            self.assertFalse(proven(shadowed, "ready and xs:", "ready and len(xs):"))

    def test_local_named_vars_and_reflection_alias_are_distinct(self):
        src = "def other():\n    vars = [1]\n    return vars\ndef f():\n    xs = []\n    if xs:\n        return 1\n"
        self.assertTrue(proven(src, "if xs:", "if len(xs):"))
        self.assertFalse(proven("read_frame = vars\n" + src, "if xs:", "if len(xs):"))
