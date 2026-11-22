import ast
import difflib
import gzip
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import class_checks
from generate_class_checks import ClassRecorder, Transform
from impact import parse_patch


class TestClassChecks(unittest.TestCase):
    def measure(self, old, new, scenarios, *, namespace=None, exp=None, args="value", loading=False):
        def program(check):
            return f"def f({args}):\n    return isinstance(value, {check})\n"
        before, after = program(old), program(new)
        p = parse_patch("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                    fromfile="a/model.py", tofile="b/model.py")))[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recorder = ClassRecorder(root, sorted(scenarios))
            transform = Transform(recorder, "model.py")
            tree = transform.visit(ast.parse(before))
            ns = {"_br_class_checks": transform.probes, **(namespace or {})}
            if exp is not None:
                ns["exp"] = exp
            exec(compile(ast.fix_missing_locations(tree), "model.py", "exec"), ns)
            with patch.dict(sys.modules, {"sqlglot.expressions": exp} if exp else {}):
                for name, prepare in scenarios.items():
                    recorder.activate(recorder.bits[name])
                    recorder.loading = int(loading)
                    ns["f"](*prepare(ns))
                    recorder.passed.add(name)
            out = root / "facts.gz"
            recorder.save(out)
            with gzip.open(out, "rt") as stream:
                data = json.load(stream)
        data["pool"] = [int(i, 16) for i in data["pool"]]
        data["lines"] = {"model.py": {2}}
        with patch.object(class_checks, "profile", return_value=data):
            return class_checks.select(p, before, set(scenarios))

    def test_ordinary_subclasses_nested_tuples_and_spoofed_class(self):
        calls = []
        class Proxy:
            @property
            def __class__(self):
                calls.append(True)
                return int
        cases = {"integer": lambda _: (3,), "boolean": lambda _: (True,), "string": lambda _: ("x",),
                 "list": lambda _: ([],), "proxy": lambda _: (Proxy(),)}
        found = self.measure("int", "(int, (str, bytes))", cases)
        self.assertEqual(found, {"string", "proxy"})
        self.assertEqual(calls, [True])

    def test_runtime_aliases_and_unobserved_new_abc(self):
        class A: pass
        class B: pass
        A.__module__ = B.__module__ = "sqlglot.example"
        e = types.ModuleType("sqlglot.expressions")
        e.A, e.B = A, B
        def swap(_):
            e.B = A
            return (B(),)
        self.assertEqual(self.measure("exp.A", "exp.B", {"before": lambda _: (B(),), "after": swap}, exp=e),
                         {"before"})
        class Meta(type):
            def __instancecheck__(cls, value):
                raise AssertionError("the profiler invoked a metaclass")
        class ABC(metaclass=Meta): pass
        ABC.__module__ = "sqlglot.example"
        e.ABC = ABC
        self.assertIsNone(self.measure("exp.A", "exp.ABC", {"opaque": lambda _: (B(),)}, exp=e))

    def test_proposed_local_rhs_remains_opaque(self):
        self.assertIsNone(self.measure("int", "str", {"shadow": lambda _: (1, int)}, args="value, str"))

    def test_namespace_collision_is_opaque(self):
        class Key:
            def __hash__(self):
                return hash("str")
            def __eq__(self, other):
                raise AssertionError("the profiler probed a colliding key")
        self.assertIsNone(self.measure("int", "str", {"opaque": lambda _: ([],)}, namespace={Key(): True}))

    def test_import_time_and_unrelated_edit(self):
        self.assertIsNone(self.measure("int", "str", {"init": lambda _: (1,)}, loading=True))
        before = "def f(a):\n    return isinstance(a, int) or True\n"
        after = "def f(a):\n    return isinstance(a, str) or False\n"
        p = parse_patch("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                     fromfile="a/model.py", tofile="b/model.py")))[0]
        self.assertIsNone(class_checks.edited_check(p, before))


if __name__ == "__main__":
    unittest.main()
