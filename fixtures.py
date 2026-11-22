"""Select fixture consumers and changed test methods in the pinned SQLGlot tree."""

import ast
import json
from collections import defaultdict
from pathlib import Path


INDEX = Path(__file__).with_name("maps") / "fixtures.json"
FIXTURES = "tests/fixtures/"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def build_index(base):
    """Trace literal fixture arguments in SQLGlot's test helpers and unittest methods."""
    owners = defaultdict(set)
    imports = defaultdict(set)
    for source in sorted((base / "tests").rglob("*.py")):
        path = source.relative_to(base).as_posix()
        tree = ast.parse(source.read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tests."):
                imports[node.module.replace(".", "/") + ".py"].add(path)
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for fn in (node for node in cls.body if isinstance(node, FUNCTIONS)):
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    callee = node.func
                    name = getattr(callee, "attr", None) or getattr(callee, "id", None)
                    literal = node.args[0]
                    if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
                        continue
                    if name == "check_file":
                        relative = f"optimizer/{literal.value}.sql"
                    elif name in {"load_sql_fixture_pairs", "load_sql_fixtures"}:
                        relative = literal.value
                    else:
                        continue
                    if fn.name.startswith("test_"):
                        owners[FIXTURES + relative].add(f"{path}::{cls.name}::{fn.name}")
                    elif fn.name == "setUpClass":
                        owners[FIXTURES + relative].add(f"{path}::{cls.name}::*")
    return {
        "sql": {key: sorted(ids) for key, ids in sorted(owners.items())},
        "imports": {key: sorted(paths) for key, paths in sorted(imports.items())},
    }


def _split_test_bodies(text):
    """A module skeleton keeps decorators, bases, defaults, and collection code intact."""
    tree = ast.parse(text)
    bodies = {}

    def collect(node, prefix):
        for fn in node.body:
            if isinstance(fn, ast.ClassDef):
                collect(fn, prefix + fn.name + "::")
            elif isinstance(fn, FUNCTIONS) and fn.name.startswith("test_"):
                bodies[prefix + fn.name] = tuple(ast.dump(s) for s in fn.body)
                fn.body = []

    collect(tree, "")
    return ast.dump(tree), bodies


def _sql_fixture_text(text):
    # SQLGlot's tests.helpers._filter_comments ignores full lines beginning with --.
    return "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))


class FixtureSelector:
    def __init__(self, base_path, test_ids, deps):
        self.base = Path(base_path)
        self.ids = set(test_ids)
        self.deps = deps
        self.index = json.loads(INDEX.read_bytes())

    def _test_modules(self, path):
        paths = {path}
        pending = [path]
        while pending:
            for dependent in self.index["imports"].get(pending.pop(), ()):
                if dependent not in paths:
                    paths.add(dependent)
                    pending.append(dependent)
        return {id_ for id_ in self.ids if id_.split("::", 1)[0] in paths}

    def select(self, patch):
        path = patch.old_path or patch.path
        if path.startswith(FIXTURES):
            readers = set(self.deps.fixture(path))
            sql_readers = self.index["sql"].get(path, ())
            if sql_readers and patch.old_path == patch.new_path:
                old = (self.base / path).read_text(encoding="utf-8")
                if _sql_fixture_text(old) == _sql_fixture_text(patch.apply(old)):
                    return set()
            for name in sql_readers:
                if name.endswith("::*"):
                    readers.update(id_ for id_ in self.ids if id_.startswith(name[:-1]))
                else:
                    readers.add(name)
            return readers & self.ids

        if not (path.startswith("tests/") and path.endswith(".py")):
            return None
        if patch.old_path != patch.new_path:
            return self._test_modules(path)
        old = (self.base / path).read_text(encoding="utf-8")
        new = patch.apply(old)
        try:
            # Some invalid scopes parse successfully and only fail when compiled.
            compile(new, path, "exec", dont_inherit=True)
            skeleton, old_methods = _split_test_bodies(old)
            changed_skeleton, new_methods = _split_test_bodies(new)
        except (SyntaxError, ValueError):
            return self._test_modules(path)
        if skeleton != changed_skeleton:
            return self._test_modules(path)
        changed = {
            f"{path}::{name}" for name, before in old_methods.items()
            if before != new_methods[name]
        }
        return {id_ for id_ in self.ids if id_.split("[", 1)[0] in changed}


if __name__ == "__main__":
    import sys

    INDEX.write_text(json.dumps(build_index(Path(sys.argv[1])), indent=2) + "\n")
