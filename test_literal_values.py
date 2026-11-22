import ast
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import literal_values
from generate_literal_values import Literals, Transform


class TestLiteralValues(unittest.TestCase):
    def profile(self, expression, inputs):
        source = f"def decide(x):\n    return {expression}\n"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            recorder = Literals(root, sorted(inputs))
            transform = Transform(recorder, "model.py")
            tree = transform.visit(ast.parse(source))
            ns = {"_br_literal_values": transform.probes}
            exec(compile(ast.fix_missing_locations(tree), "model.py", "exec"), ns)
            for name, values in inputs.items():
                recorder.activate(recorder.bits[name])
                for value in values:
                    ns["decide"](value)
                recorder.passed.add(name)
            output = root / "facts.gz"
            recorder.save(output)
            with gzip.open(output, "rt") as f:
                return json.load(f)

    def select(self, profile, before, after):
        def expr(source):
            return ast.parse(f"def decide(x):\n    return {source}\n").body[0].body[0].value
        with patch.object(literal_values, "profile", return_value=profile):
            return literal_values.affected("model.py", expr(before), expr(after))

    def test_membership_and_substring(self):
        values = {"first": ["a"], "second": ["b"], "third": ["c"]}
        profile = self.profile("x in ('a', 'c')", values)
        self.assertEqual(self.select(profile, "x in ('a', 'c')", "x in ('b', 'c')"), {"first", "second"})
        profile = self.profile("'a' in x", values)
        self.assertEqual(self.select(profile, "'a' in x", "'b' in x"), {"first", "second"})

    def test_nonprimitive_and_overflow_remain_selected(self):
        class Opaque:
            def __eq__(self, other):
                return False
        profile = self.profile("x == 150", {"wide": range(170), "opaque": [Opaque()], "zero": [0]})
        self.assertEqual(self.select(profile, "x == 150", "x == 163"), {"wide", "opaque"})
        self.assertIsNone(self.select(profile, "x == 150", "abs(x) == 163"))

    def test_ordered_version_tuples(self):
        observed = {"past": [(1, 0)], "same": [(1, 3)], "later": [(4, 0)]}
        profile = self.profile("x >= (1, 3)", observed)
        self.assertEqual(self.select(profile, "x >= (1, 3)", "x >= (2, 0)"), {"same"})


if __name__ == "__main__":
    unittest.main()
