import ast
import difflib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_optimizer_witness as build
import optimizer_witness as witness
from impact import parse_patch


def expression(source):
    return ast.parse(source, mode="eval").body


class TestOptimizerWitness(unittest.TestCase):
    def test_call_destinations_and_evaluation_order(self):
        shape = ("sqlglot.example", "f", ("x", "y"), 0, ("size",), 0, (), True)
        before = expression("f(next_value(), other_value(), size=width())")
        for source, expected in [
            ("f(x=next_value(), y=other_value(), size=width())", True),
            ("f(y=other_value(), x=next_value(), size=width())", False),
            ("f(y=next_value(), x=other_value(), size=width())", False),
            ("f(*args, size=width())", False),
            ("f(x=next_value(), y=other_value(), **width())", False),
            ("g(x=next_value(), y=other_value(), size=width())", False),
            ("f(x=next_value(), extra=other_value(), size=width())", False),
        ]:
            with self.subTest(source=source):
                self.assertEqual(
                    witness.equivalent_call(before, expression(source), shape), expected
                )
        positional_only = (*shape[:3], 1, *shape[4:])
        self.assertFalse(
            witness.equivalent_call(
                before,
                expression("f(x=next_value(), y=other_value(), size=width())"),
                positional_only,
            )
        )

    def test_python_exceptions_observe_same_arguments(self):
        before = "f(emit(1), emit(2), pos=emit(3))"
        after = "f(x=emit(1), y=emit(2), pos=emit(3))"

        def f(x, y, **extra):
            return x, y, list(extra.items())

        def evaluate(source, cancel):
            order = []

            def emit(i):
                order.append(i)
                if cancel == i:
                    raise ArithmeticError
                return i

            try:
                result = eval(source, {"f": f, "emit": emit})
            except ArithmeticError:
                result = "exception"
            return result, order

        for stop in (None, 1, 2, 3):
            self.assertEqual(evaluate(before, stop), evaluate(after, stop))

    def test_scope_or_compound_changes_are_rejected(self):
        source = "def example(value):\n    local = value\n    return local\n"
        path = "sqlglot/optimizer/_example.py"

        def changed(prior, replacement):
            revised = source.replace(prior, replacement)
            (patch,) = parse_patch(
                "".join(
                    difflib.unified_diff(
                        source.splitlines(True),
                        revised.splitlines(True),
                        "a/" + path,
                        "b/" + path,
                    )
                )
            )
            return witness.changed_assignment(patch, source)

        self.assertIsNone(
            changed(
                "local = value", "local = (yield value) if value is None else value"
            )
        )
        self.assertIsNone(
            changed(
                "local = value", "local = (shadow := value) if value is None else value"
            )
        )
        self.assertIsNone(
            changed(
                "local = value\n    return local",
                "local = None if value is None else value\n    return None",
            )
        )
        self.assertIsNotNone(
            changed("local = value", "local = None if value is None else value")
        )

    def test_predicates_require_unanimous_observed_values(self):
        node = expression('type(schema.dialect).__name__ == "Redshift"')
        self.assertIs(
            witness.condition(node, "schema", {"dialect": [["Default", False]]}), False
        )
        self.assertIsNone(
            witness.condition(
                node, "schema", {"dialect": [["Default", False], ["Redshift", False]]}
            )
        )
        self.assertIsNone(
            witness.condition(node, "schema", {"dialect": [[None, False]]})
        )
        self.assertIsNone(witness.condition(node, "schema", {}))
        self.assertIsNone(
            witness.condition(
                expression("schema == None"), "schema", {"": [["Default", False]]}
            )
        )
        self.assertIs(
            witness.condition(
                expression("not (schema is None)"), "schema", {"": [["Default", False]]}
            ),
            True,
        )

    def test_property_and_receiver_interception(self):
        class Receiver:
            def __init__(self):
                self._current = []

            @property
            def current(self):
                return self._current

        Receiver.__module__ = "sqlglot.example"
        obj = Receiver()
        self.assertEqual(
            build.attributes(obj, ("current",))["current"], ["list", False]
        )

        def forbidden(self):
            raise AssertionError("The profiler called an unclassified getter")

        with patch.object(Receiver, "current", property(forbidden)):
            self.assertNotIn("current", build.attributes(obj, ("current",)))

        def intercepted(self, name):
            return object.__getattribute__(self, name)

        with patch.object(Receiver, "__getattribute__", intercepted):
            self.assertNotIn("current", build.attributes(obj, ("current",)))

    def test_callable_must_be_current_pinned_python_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            file = root / "sqlglot/example.py"
            mod = types.ModuleType("sqlglot.example")
            exec(compile("def f(x, **kw): return x", str(file), "exec"), mod.__dict__)
            fn = mod.f
            with (
                patch.object(build, "base", root, create=True),
                patch.dict(sys.modules, {mod.__name__: mod}),
            ):
                self.assertEqual(build.function_shape(fn)[2], ("x",))
                with patch.object(mod, "f", lambda x: x):
                    self.assertIsNone(build.function_shape(fn))
                with patch.object(fn, "__signature__", None, create=True):
                    self.assertIsNone(build.function_shape(fn))
                with patch.object(fn, "__wrapped__", lambda: None, create=True):
                    self.assertIsNone(build.function_shape(fn))
                self.assertIsNone(build.function_shape(len))
