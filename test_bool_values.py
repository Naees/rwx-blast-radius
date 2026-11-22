import ast
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bool_values
from generate_bool_values import BoolRecorder, Transform, compile_operand


class TestBoolValues(unittest.TestCase):
    def select(self, before, after, cases, args="a, b"):
        def program(condition):
            return ast.parse(f"def decide({args}):\n    if {condition}:\n        return 1\n    return 0\n")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recorder = BoolRecorder(root, sorted(cases))
            transform = Transform(recorder, "model.py")
            module = transform.visit(program(before))
            ns = {"_br_bool_probes": transform.probes}
            exec(compile(ast.fix_missing_locations(module), "model.py", "exec"), ns)
            for case, arguments in cases.items():
                recorder.activate(recorder.bits[case])
                ns["decide"](*arguments)
                recorder.passed.add(case)
            path = root / "facts.gz"
            recorder.save(path)
            with gzip.open(path, "rt") as stream:
                data = json.load(stream)
        with patch.object(bool_values, "profile", return_value=data):
            return bool_values.affected("model.py", program(before).body[0].body[0].test,
                                        program(after).body[0].body[0].test)

    def test_exact_builtins_and_correlated_inputs(self):
        cases = {"both": ({"x": 1}, [0]), "neither": ({}, []), "only_left": ({"x": False}, [])}
        self.assertEqual(self.select("a.get('x') is not None and len(b) > 0",
                                     "a.get('x') is not None or len(b) > 0", cases), {"only_left"})

    def test_skipped_descriptors_and_custom_truth_are_never_invoked(self):
        class Opaque:
            def __bool__(self):
                raise AssertionError("attempted custom truth")
            @property
            def value(self):
                raise AssertionError("attempted descriptor lookup")
        for right in ["b", "b.value"]:
            self.assertEqual(self.select(f"a and {right}", f"a or {right}", {"opaque": (False, Opaque())}), {"opaque"})

    def test_hostile_dict_keys_are_never_compared(self):
        class CollidingKey:
            def __hash__(self):
                return hash("same")
            def __eq__(self, other):
                raise AssertionError("attempted equality")
        inputs = {"unsafe": (False, {CollidingKey(): 0}), "safe": (False, {"same": 0})}
        self.assertEqual(self.select("a and b.get('same')", "a or b.get('same')", inputs), {"unsafe"})

    def test_shadowed_builtin_and_allocated_identity(self):
        def custom_len(_):
            raise AssertionError("attempted custom len")
        self.assertEqual(self.select("a and len(b)", "a or len(b)",
                                     {"shadow": (False, [], custom_len)}, "a, b, len"), {"shadow"})
        self.assertIsNone(compile_operand(ast.parse("len(a) is len(b)", mode="eval").body))

    def test_nested_sequence_in_true_condition(self):
        cases = {"unchanged": (True, False, True), "changed": (False, True, True)}
        self.assertEqual(self.select("(a and b) or c", "a and (b or c)", cases, "a, b, c"), {"changed"})


if __name__ == "__main__":
    unittest.main()
