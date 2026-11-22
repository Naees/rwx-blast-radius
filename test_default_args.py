import difflib
import tempfile
import sys
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

    def source_readers(self, path):
        return ()


class TestDefaultArguments(unittest.TestCase):
    def test_bound_values_retain_matching_unmeasured_and_new_tests(self):
        data = ({"matches": 1, "explicit": 2, "unmeasured": 4}, 4, {"p:4:6": "1"})
        callers = {"matches", "explicit", "unmeasured", "new"}
        with patch.object(default_args, "_bound_values", return_value=data):
            self.assertEqual(
                default_args._possible_default(callers, "p", 4, 6),
                {"matches", "unmeasured", "new"},
            )
            self.assertEqual(
                default_args._possible_default(callers, "p", 8, 6), callers
            )

    def test_entry_probe_only_compares_exact_scalar_types(self):
        from generate_default_values import DefaultValues

        class Other:
            def __eq__(self, value):
                raise AssertionError("custom equality was called")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p = root / "sqlglot" / "sample.py"
            p.parent.mkdir()
            source = "def run(flag=True):\n    return flag\n"
            p.write_text(source)
            recorder = DefaultValues(
                root, ["explicit", "matches"], default_args.build(root)
            )
            namespace = {}
            exec(compile(source, str(p), "exec"), namespace)
            run = namespace["run"]
            sys.monitoring.use_tool_id(recorder.slot, "test default observer")
            try:
                sys.monitoring.register_callback(
                    recorder.slot, sys.monitoring.events.PY_START, recorder.enter
                )
                recorder.attach("sqlglot/sample.py", run.__code__)
                recorder.activate(recorder.bits["explicit"])
                run(False)
                run(Other())
                recorder.activate(recorder.bits["matches"])
                run()
                recorder.passed.update(recorder.tests)
                self.assertEqual(list(recorder.snapshot()["masks"].values()), ["0x2"])
            finally:
                sys.monitoring.set_local_events(recorder.slot, run.__code__, 0)
                sys.monitoring.register_callback(
                    recorder.slot, sys.monitoring.events.PY_START, None
                )
                sys.monitoring.free_tool_id(recorder.slot)

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

    def test_early_equivalence_keeps_signed_zeros_distinct(self):
        old = "def read(number=32):\n    return number\n"
        self.assertEqual(
            self.select(old, old.replace("32", "0x20"), Coverage({2: {"reader"}})),
            set(),
        )
        old = old.replace("32", "0.0")
        self.assertEqual(
            self.select(old, old.replace("0.0", "-0.0"), Coverage({2: {"reader"}})),
            {"reader"},
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

    def test_unused_method_default_handles_unrelated_local_with_same_name(self):
        old = "class Stub:\n    def order(self, reverse=False):\n        raise NotImplementedError\n"
        local = "def label():\n    order = 1\n    return order\n"
        self.assertEqual(
            self.select(old, old.replace("False", "True"), Coverage({}), local), set()
        )
        for extra in [
            "callback = Stub.order\n",
            "callback = vars(Stub)['order']\n",
            "callback = Stub.__dict__.get('order')\n",
            "def inspect_error(exc):\n    return exc.__traceback__.tb_frame.f_locals\n",
            "NotImplementedError = custom_exception\n",
        ]:
            self.assertIsNone(
                self.select(old, old.replace("False", "True"), Coverage({}), extra)
            )

    def test_decorator_is_proven_identity(self):
        cls = "@mark\nclass Stub:\n    def score(self, flag=False):\n        return 1\n"
        for body, expected in [
            ("return cls", set()),
            ("return inspect_and_copy(cls)", None),
        ]:
            old = f"def mark(cls):\n    {body}\n\n{cls}"
            self.assertEqual(
                self.select(old, old.replace("False", "True"), Coverage({})), expected
            )

    def test_early_rule_rejects_edited_body_and_observable_calls(self):
        old = (
            "class Stub:\n    def order(self, reverse=False):\n        return vars()\n"
        )
        changed = old.replace("False", "True")
        self.assertIsNone(self.select(old, changed, Coverage({})))
        self.assertIsNone(
            self.select(
                old, changed.replace("vars()", "None"), Coverage({3: {"caller"}})
            )
        )
        self.assertIsNone(
            self.select(
                old,
                old.replace("False", "False, other=True"),
                Coverage({3: {"caller"}}),
            )
        )


if __name__ == "__main__":
    unittest.main()
