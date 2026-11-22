import ast
import difflib
import types
import unittest

from generate_sequence_index import Sequences, Transform
from impact import parse_patch
from sequence_index import equivalent, select


class SequenceIndexTest(unittest.TestCase):
    def test_shapes_use_identity_and_normalize_negative_positions(self):
        self.assertTrue(equivalent([2, [1, 2]], -1, 1, False))
        self.assertFalse(equivalent([2, [1, 2]], 0, 1, True))
        self.assertTrue(equivalent([2, [1, 1]], 0, 1, False))
        self.assertFalse(equivalent([1, [0]], 0, 9, True))
        self.assertTrue(equivalent([1, [0]], 8, 9, True))
        self.assertFalse(equivalent([1, [0]], 0, 9, False))
        self.assertFalse(equivalent([1, [0]], 2, 3, False))
        self.assertTrue(equivalent([32, []], -1, 31, False))
        self.assertFalse(equivalent([32, []], 1, 2, False))

    def test_opaque_sequence_and_shadowed_helper(self):
        profile = Sequences.__new__(Sequences)
        profile.sites = {}
        profile.active = 1
        profile.loading = 0
        callee, capture = profile.probes("site")
        class Fake(list):
            def __iter__(self):
                raise AssertionError
        self.assertIsInstance(capture(Fake()), Fake)
        profile.active = 2
        f = lambda *a: None
        self.assertIs(callee(f), f)
        self.assertEqual(profile.sites["site"]["opaque"], 3)

    def test_only_exact_container_types_without_user_equality(self):
        p = Sequences.__new__(Sequences)
        p.sites, p.active, p.loading = {}, 1, 0
        class Meta(type):
            def __eq__(cls, other):
                raise AssertionError("metaclass equality was invoked")
        class Odd(metaclass=Meta): pass
        class Element:
            def __eq__(self, other):
                raise AssertionError("element equality was invoked")
        e = Element()
        _, capture = p.probes("sequence")
        capture(Odd())
        p.active = 2
        capture([e, None, e])
        self.assertEqual(p.sites["sequence"]["opaque"], 1)
        self.assertEqual(p.sites["sequence"]["values"], {(3, (1, 0, 1)): 2})

    def test_receiver_evaluated_once_and_can_change_exception_binding(self):
        code = "def seq_get(seq, index):\n    try:\n        return seq[index]\n    except IndexError:\n        return None\n"
        helper = types.ModuleType("helper")
        exec(compile(code, "helper.py", "exec"), vars(helper))
        p = Sequences.__new__(Sequences)
        p.sites, p.active, p.loading = {}, 1, 0
        p.bind(helper, ast.parse(code))
        self.assertTrue(p.handler())
        transform = Transform(p, "model.py")
        tree = transform.visit(ast.parse("def f(make):\n    return seq_get(make(), 2)\n"))
        ns = {"_br_sequence_index":transform.probes, "seq_get":helper.seq_get}
        exec(compile(ast.fix_missing_locations(tree), "model.py", "exec"), ns)
        calls = []
        def make():
            calls.append("once")
            helper.IndexError = ValueError
            return []
        with self.assertRaises(IndexError):
            ns["f"](make)
        self.assertEqual(calls, ["once"])
        self.assertEqual(list(p.sites.values())[0]["opaque"], 1)

    def test_cannot_drop_other_edits_or_unobserved_tests(self):
        names = ["same", "different", "opaque", "never_observed"]
        s = "def f(x):\n    return x[0]\n"
        file = "sqlglot/example.py"
        p = lambda new: parse_patch("".join(difflib.unified_diff(s.splitlines(True), new.splitlines(True), "a/"+file,"b/"+file)))[0]
        data = {file: {"2:11:item": [2, [[[1,[1]],0],[[2,[1,2]],1]]]}}, names, set(), [1,2,4]
        self.assertEqual(select(p(s.replace("[0]", "[-1]")), s, set(names), data), set(names[1:]))
        self.assertEqual(select(p(s.replace("[0]", "[-1]")), s, set(names), data, source_readers={"same"}), set(names))
        self.assertIsNone(select(p(s.replace("f(x)", "g(x)").replace("[0]", "[-1]")), s, set(names), data))
        two = "def f(x, y):\n    return x[0] + y[0]\n"
        d = parse_patch("".join(difflib.unified_diff(two.splitlines(True), two.replace("[0]", "[1]").splitlines(True),
                                                     "a/" + file, "b/" + file)))[0]
        self.assertIsNone(select(d, two, set(names), data))
        nested = "def f(x):\n    return x[0][0]\n"
        d = parse_patch("".join(difflib.unified_diff(nested.splitlines(True), nested.replace("[0]", "[1]").splitlines(True),
                                                     "a/" + file, "b/" + file)))[0]
        self.assertIsNone(select(d, nested, set(names), data))
