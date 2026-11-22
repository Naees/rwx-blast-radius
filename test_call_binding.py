import ast
import difflib
import hashlib
import unittest
from unittest.mock import Mock, patch
from generate_call_binding import (
    bindings,
    direct_sites,
    mutations,
    unsafe_project_write,
)
from call_binding import select
from impact import parse_patch

SOURCE = """from sqlglot.example_schema import ensure_schema


def annotate(schema, dialect):
    schema = ensure_schema(schema, dialect=dialect)
    return schema
"""
PATH = "sqlglot/example_annotator.py"
CALL = "schema = ensure_schema(schema, dialect=dialect)"
EQUIVALENT = "schema = ensure_schema(schema=schema, dialect=dialect)"


def rewrite(replacement):
    changed = SOURCE.replace(CALL, replacement)
    (result,) = parse_patch(
        "".join(
            difflib.unified_diff(
                SOURCE.splitlines(True),
                changed.splitlines(True),
                "a/" + PATH,
                "b/" + PATH,
            )
        )
    )
    return result


class TestBinding(unittest.TestCase):
    def setUp(self):
        self.deps = Mock()
        self.deps.tests = {"ordinary", "mocked", "source_reader", "child", "reflection"}
        self.deps.import_initializers.return_value = set()
        self.deps.source_readers.return_value = {"source_reader"}
        self.hits = {"ordinary", "mocked", "unmeasured"}
        shape = [None, "ensure_schema", ["schema"], 0, [], 0, [], True]
        observed = ["ensure_schema", "sqlglot.example_schema", "ensure_schema", shape]
        digest = hashlib.sha256(SOURCE.encode()).hexdigest()
        synthetic_index = {
            "files": {PATH: [digest, {"5:13": observed}]},
            "uncertain": ["mocked", "unused_mock"],
            "outside": ["child", "reflection"],
        }
        self.enterContext(patch("call_binding.index", return_value=synthetic_index))
        self.enterContext(patch("impact._scope_index", return_value=None))

    def match(self, value):
        return select(rewrite(value), SOURCE, self.deps, self.hits)

    def test_equivalent_keyword_preserves_uncertain_candidates(self):
        self.assertEqual(
            self.match(EQUIVALENT),
            {"mocked", "child", "reflection", "source_reader", "unmeasured"},
        )
        self.deps.import_initializers.assert_called_once_with(PATH, {5})
        self.deps.source_readers.assert_called_once_with(PATH)

    def test_observable_edits(self):
        for edit in (
            "schema = ensure_schema(dialect=dialect, schema=schema)",
            "schema = ensure_schema(*schema, dialect=dialect)",
            "schema = ensure_schema(schema=schema, **dialect)",
            "schema = ensure_schema(schema=1, dialect=dialect)",
            "schema = ensure_schema(s=schema, dialect=dialect)",
            "other = ensure_schema(schema=schema, dialect=dialect)",
        ):
            with self.subTest(edit=edit):
                self.assertIsNone(self.match(edit))

    def test_import_call_unproven(self):
        self.deps.import_initializers.return_value = {"sqlglot/__init__.py"}
        self.assertIsNone(self.match(EQUIVALENT))

    def test_conditional_rebinding_and_variadics(self):
        src = """def f(x): return x
if unknown: f=another
@wrapper
def g(x): return x
def h(x,/): return x
def k(*args): return args
def plain(x): return x
"""
        t = ast.parse(src)
        fs, _ = bindings(t, compile(t, "t", "exec"))
        self.assertEqual(set(fs), {"plain"})

    def test_shadowed_function_parameter(self):
        src = "from project import f\ndef caller(f): return f(1)\n"
        t = ast.parse(src)
        self.assertEqual(list(direct_sites(t, compile(t, "t", "exec"))), [])

    def test_external_rebindings(self):
        self.assertIn("f", mutations(ast.parse("mock.patch('some.path.f')")))
        self.assertIn("f", mutations(ast.parse("module.f = replacement")))
        self.assertTrue(
            unsafe_project_write(ast.parse("setattr(module, unknown, wrapper)"))
        )
        self.assertTrue(unsafe_project_write(ast.parse("f.__code__ = unknown")))
