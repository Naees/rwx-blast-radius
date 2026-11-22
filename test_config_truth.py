import ast
import difflib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config_truth
from generate_config_truth import FlagReads, guards
from impact import parse_patch


class TestConfigTruth(unittest.TestCase):
    @staticmethod
    def patch(source, changed):
        return parse_patch("".join(difflib.unified_diff(source.splitlines(True), changed.splitlines(True),
                                                       fromfile="a/model.py", tofile="b/model.py")))[0]

    def test_missing_opaque_reads_and_source_readers_survive(self):
        source = "class Engine:\n    FLAG = True\n"
        p = self.patch(source, source.replace("True", "False"))
        # Rows 3 and 4 have observed reads. Row 4 is a different unproven
        # consumer; an otherwise safe case that also read it must be kept.
        nodes = ["good", "changed", "unknown", "disagree", "opaque", "unseen", "alternate", "external", "source"]
        bits = {n: 1 << i for i, n in enumerate(nodes)}
        pool = (0, bits["opaque"], sum(bits[n] for n in nodes if n not in ("opaque", "unseen")), bits["alternate"])
        names = "model.Engine.FLAG"
        tree = [True, [0, 1]]
        data = {"tests": nodes, "incomplete": ["external"], "symbols": {names: [True, 1, [[3, ["g", tree, 0], 2], [4, None, 3]]]}}
        conditions = {"tests": nodes, "incomplete": [],
                      "pool": ["0", format(bits["good"] | bits["external"] | bits["source"] | bits["alternate"], "x"),
                               format(bits["changed"], "x"), format(bits["unknown"], "x")],
                      "sites": {"g": [0, {"?F": 1, "?T": 2, "??": 3}]}}
        imp = SimpleNamespace(global_change=True, changed_names={"FLAG"}, classes=("Engine",))
        deps = SimpleNamespace(source_readers=lambda path: {"source"})
        with patch.object(config_truth, "profile", return_value=(data, conditions, pool)), patch.object(config_truth, "context", return_value=True):
            self.assertEqual(config_truth.select(p, imp, source, deps, set(nodes)), set(nodes) - {"good"})

    def test_only_literal_flip_and_original_context(self):
        source = "class Engine:\n    FLAG = 1\n"
        self.assertIsNone(config_truth.flipped(self.patch(source, source.replace("1", "0"))))
        source = "class Engine:\n    FLAG = True\n    OTHER = True\n"
        self.assertIsNone(config_truth.flipped(self.patch(source, source.replace("True", "False"))))

    def test_guard_requires_exactly_one_direct_occurrence(self):
        text = "def f(self, x):\n    if self.FLAG and x:\n        return 1\n    if self.FLAG or self.FLAG:\n        return 2\n"
        t = ast.parse(text)
        from bool_values import location
        bs = {location("model.py", n): True for n in ast.walk(t) if isinstance(n, ast.BoolOp)}
        parents = {c: n for n in ast.walk(t) for c in ast.iter_child_nodes(n)}
        self.assertIsNotNone(guards("model.py", "FLAG", {2, 4}, t, parents, bs)[2])
        self.assertIsNone(guards("model.py", "FLAG", {2, 4}, t, parents, bs)[4])
        tree = ast.parse("if FLAG:\n    pass\n")
        self.assertEqual(guards("model.py", "FLAG", {1}, tree, {}, {}), {})

    def test_no_user_hook_during_provenance_lookup(self):
        class Plain: FLAG = True
        class Meta(type):
            def __hash__(self):
                raise AssertionError("metaclass hash was called")
        class Dynamic(metaclass=Meta):
            @property
            def FLAG(self):
                raise AssertionError("descriptor was evaluated")
        symbol = Plain.__module__ + "." + Plain.__qualname__ + ".FLAG"
        r = FlagReads(Path.cwd(), ["test"], {symbol: ("model.py", {1: None}, True)})
        self.assertEqual(r.receiver(Plain, "FLAG"), (symbol, True))
        self.assertEqual(r.receiver(Dynamic, "FLAG"), (None, False))


if __name__ == "__main__":
    unittest.main()
