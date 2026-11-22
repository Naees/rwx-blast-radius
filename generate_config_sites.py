"""Find fully represented predicate reads of pinned parser and generator scalar flags."""
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
from collections import defaultdict
from pathlib import Path
import subprocess
import sys

import pytest

from configmap import literal, members, module_members, module_name
from generate_config import Instrument, Profile
from generate_map import PINNED
from mapdata import load
from query_checks import LocalPool

MAPS = Path(__file__).resolve().parent / "maps"


def predicate_site(node, parents):
    """A transient parser/generator decision, without storing the config value."""
    child = node
    parent = parents.get(node)
    while isinstance(parent, (ast.Compare, ast.BoolOp)) or (
        isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not)
    ):
        child, parent = parent, parents.get(parent)
    if not isinstance(parent, (ast.If, ast.IfExp)) or parent.test is not child:
        return False
    fn = parent
    while fn is not None and not isinstance(fn, (ast.ClassDef, ast.FunctionDef, ast.Lambda)):
        fn = parents.get(fn)
    if not isinstance(fn, ast.FunctionDef) or fn.decorator_list or not fn.args.args:
        return False
    if fn.args.args[0].arg != "self" or not isinstance(parents.get(fn), ast.ClassDef):
        return False
    if not (fn.name.endswith("_sql") or fn.name.startswith("_parse_")):
        return False
    for n in ast.walk(fn):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return False
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in {"setattr", "delattr"}:
            return False
        if isinstance(n, ast.Attribute) and ("cache" in n.attr.lower() or n.attr in {"__class__", "__dict__"}):
            return False
    return True


def discover(root):
    constants = load(MAPS / "constants.json.gz")
    symbols = set(constants["symbols"])
    names = {s.rsplit(".", 1)[1] for s in symbols}
    unstable, readers = set(), defaultdict(set)
    classes, safe_defs = set(), set()
    for path in sorted([*(root / "sqlglot").rglob("*.py"), *(root / "tests").rglob("*.py")]):
        rel = path.relative_to(root).as_posix()
        module = module_name(rel)
        tree = ast.parse(path.read_bytes())
        parents = {child: p for p in ast.walk(tree) for child in ast.iter_child_nodes(p)}
        if rel.startswith("sqlglot/"):
            defs = list(members(tree, module))
            classes.update(s for s, _ in defs)
            for sym, stmt in [*defs, *module_members(tree, module)]:
                if type(literal(stmt.value)) in {bool, int, float, str}:
                    safe_defs.add(sym)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in names:
                # Reflection, an explicit dictionary key or an indirect field reference.
                unstable.add(n.value)
            if isinstance(n, ast.ImportFrom):
                unstable.update(a.name for a in n.names if a.name in names)
            if not isinstance(n, (ast.Attribute, ast.Name)):
                continue
            attr = n.attr if isinstance(n, ast.Attribute) else n.id
            if attr not in names:
                continue
            if not isinstance(n.ctx, ast.Load):
                if isinstance(n, ast.Attribute):
                    unstable.add(attr)
                continue
            if not rel.startswith("sqlglot/") or not predicate_site(n, parents):
                unstable.add(attr)
            else:
                readers[attr].add((rel, n.lineno, n.end_lineno))
    result = {}
    for s in sorted((symbols & safe_defs)):
        attr = s.rsplit(".", 1)[1]
        static = constants["bare"].get(attr, []) + constants["qualified"].get(attr, [])
        if attr in unstable or not static or None in static:
            continue
        sites = {tuple(entry) for entry in static}
        if sites != readers[attr] or len({p for p, _, _ in sites}) != 1:
            continue
        result[s] = sorted(sites)
    return result, classes


class Sites(Profile):
    def __init__(self, root, nodes, defs, classes):
        super().__init__(root, nodes)
        self.scalars = set(defs) & classes
        self.attr_names = {name.rsplit(".", 1)[1] for name in self.scalars}
        self.locations = defaultdict(set)

    def activate(self, mask):
        self.active = mask

    def read_attr(self, obj, attr, *default):
        value = getattr(obj, attr, *default)
        if attr in self.attr_names:
            cls = obj if isinstance(obj, type) else type(obj)
            symbol = self.owner(cls, attr)
            if symbol:
                caller = sys._getframe(1)
                path = self.relative(caller.f_code.co_filename)
                if not self.active or not path:
                    self.unsafe.add(symbol)
                else:
                    self.locations[symbol].add((path, caller.f_lineno))
        return value


def main():
    root, inventory, output = [Path(a).resolve() for a in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Unexpected SQLGlot revision")
    subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"], check=True)
    defs, classes = discover(root)
    print("Statically bounded scalar definitions:", len(defs), flush=True)
    rows = json.loads(inventory.read_bytes())["tests"]
    nodes = sorted(r["id"] for r in rows if (
        (r["id"].startswith("tests/dialects/") and r["seconds"] <= 1.5) or
        (r["id"].startswith("tests/test_optimizer.py::") and r["seconds"] <= 2.6)
    ))
    profile = Sites(root, nodes, defs, classes)

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(self.get_data(self.path), self.path)
            return compile(ast.fix_missing_locations(Instrument(profile).visit(tree)), self.path, "exec")

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".", 1)[0] in {"sqlglot", "tests"}:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and spec.origin and spec.origin.endswith(".py"):
                    spec.loader = Loader(fullname, spec.origin)
                return spec
            return None

    builtins._br_read = profile.read_attr
    concurrent.futures.ProcessPoolExecutor = LocalPool
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests/dialects", "tests/test_optimizer.py"], plugins=[profile])
    if result not in (0, 1):
        raise RuntimeError(result)
    supported = {}
    profiled = load(MAPS / "config.json.gz")["masks"]
    for symbol, cases in defs.items():
        if symbol in profile.unsafe:
            continue
        path = cases[0][0]
        lines = {n for _, start, end in cases for n in range(start, end + 1)}
        observed = profile.locations[symbol]
        if symbol in classes and (not observed or symbol not in profiled):
            continue
        if any(p != path or n not in lines for p, n in observed):
            continue
        supported[symbol] = [path, sorted(lines)]
    good = sorted(profile.passed - profile.failed)
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps({"tests": good, "sites": supported}, separators=(",", ":")).encode())
    print("Verified", len(supported), "definitions and", len(good), "test methods", flush=True)


if __name__ == "__main__":
    main()
