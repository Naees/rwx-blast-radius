import abc
import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from generate_bool_values import BoolRecorder, SLOTS, Transform, UNKNOWN, compile_operand
from slot_facts import SlotFacts
from slot_facts import MEMBER_READ

SOURCE = """
class Expr:
    __slots__ = ('args', 'parent')
class Join(Expr): pass
class Overridden(Expr):
    @property
    def args(self):
        raise AssertionError('invoked a property')
class Dynamic(Expr):
    def __getattr__(self, name):
        raise AssertionError('invoked fallback lookup')
class DynamicGet(Expr):
    def __getattribute__(self, name):
        raise AssertionError('invoked an overridden lookup')
class Container(Expr):
    __slots__ = ('child',)
class ForeignDescriptor:
    __slots__ = ('args',)
"""


class TestSlotFacts(unittest.TestCase):
    def setUp(self):
        self.table = SlotFacts()
        self.mod = types.ModuleType("sqlglot_test_example")
        tree = ast.parse(SOURCE)
        self.table.add_module(self.mod.__name__, tree)
        exec(compile(tree, "example.py", "exec"), vars(self.mod))
        self.table.register(self.mod)

    def test_initialized_data_descriptor_overrides_instance_dict(self):
        join = self.mod.Join()
        join.args = {"on": True}
        join.__dict__["args"] = "shadow"
        self.assertIs(self.table.read(join, "args", UNKNOWN), join.args)
        child = self.mod.Container()
        child.child = join
        with patch("generate_bool_values.SLOTS", self.table):
            read = compile_operand(ast.parse("c.child.args.get('on')", mode="eval").body)
            self.assertIs(read({"c": child}, True), True)

    def test_rejects_dynamic_lookup_properties_unknown_classes_and_abc(self):
        for kind in ("Overridden", "Dynamic", "DynamicGet"):
            value = vars(self.mod)[kind]()
            self.assertIs(self.table.read(value, "args", UNKNOWN), UNKNOWN)
        class Outside(self.mod.Expr): pass
        self.assertIs(self.table.read(Outside(), "args", UNKNOWN), UNKNOWN)
        class Meta(abc.ABCMeta):
            def __hash__(self):
                raise AssertionError("attempted to hash metaclass")
        class Untrusted(metaclass=Meta): pass
        self.assertIs(self.table.read(Untrusted(), "args", UNKNOWN), UNKNOWN)
        # Descriptor stolen from an unrelated owner does not become trusted.
        self.mod.Join.args = self.mod.ForeignDescriptor.args
        self.assertIs(self.table.read(self.mod.Join(), "args", UNKNOWN), UNKNOWN)

    def test_cache_invalidation_and_missing_slot_suppresses_exception(self):
        cls = self.mod.Join
        obj = cls()
        self.assertIs(self.table.read(obj, "args", UNKNOWN), UNKNOWN)
        obj.args = "ready"
        self.assertEqual(self.table.read(obj, "args", UNKNOWN), "ready")
        def nope(_):
            raise AssertionError("invoked a patched descriptor")
        cls.args = property(nope)
        self.assertIs(self.table.read(obj, "args", UNKNOWN), UNKNOWN)
        del cls.args
        self.assertEqual(self.table.read(obj, "args", UNKNOWN), "ready")
        del obj.args
        raised = []
        def callback(frame, event, arg):
            if event == "exception" and isinstance(arg[1], AttributeError):
                raised.append(frame.f_code.co_name)
            return callback
        sys.settrace(callback)
        try:
            result = self.table.read(obj, "args", UNKNOWN)
        finally:
            sys.settrace(None)
        self.assertIs(result, UNKNOWN)
        self.assertEqual(raised, [])

    def test_skipped_descriptor_and_unknown_truth_remain_opaque(self):
        class Truth:
            def __bool__(self):
                raise AssertionError("invoked custom truth")
        class Foreign: pass
        with tempfile.TemporaryDirectory() as folder, patch("generate_bool_values.SLOTS", self.table):
            r = BoolRecorder(Path(folder), ["test"])
            transform = Transform(r, "model.py")
            code = ast.parse("def f(a, b):\n    if a and b.args.get('x'):\n        return True\n")
            tree = transform.visit(code)
            env = {"_br_bool_probes": transform.probes}
            exec(compile(ast.fix_missing_locations(tree), "model.py", "exec"), env)
            r.activate(1)
            for obj in (self.mod.Overridden(), self.mod.Join()):
                if type(obj) is self.mod.Join: obj.args = {"x": Truth()}
                self.assertIsNone(env["f"](False, obj))
            obj = self.mod.Join()
            obj.args = {"x": Foreign()}
            self.assertIsNone(env["f"](False, obj))
            state = next(iter(r.sites.values()))
            self.assertEqual(state["values"], {"F?": 1})

    def test_no_instance_dictionary_materialization(self):
        cls = self.mod.Join
        obj = cls()
        obj.other = {"on": True}
        self.assertIs(self.table.read(obj, "other", UNKNOWN), UNKNOWN)
        self.assertIs(self.table.read(obj, "__dict__", UNKNOWN), UNKNOWN)

    def test_descriptor_is_not_dispatched_after_concurrent_override(self):
        obj = self.mod.Join()
        obj.args = "ready"
        def rejected(_):
            raise AssertionError("a concurrent property should never be called")
        def replacing(member, value, cls):
            cls.args = property(rejected)
            return MEMBER_READ(member, value, cls)
        with patch("slot_facts.MEMBER_READ", replacing):
            self.assertIs(self.table.read(obj, "args", UNKNOWN), UNKNOWN)


if __name__ == "__main__":
    unittest.main()
