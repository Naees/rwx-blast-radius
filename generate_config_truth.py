"""Observe all read locations and owners of Boolean flags with bounded predicate consumers."""

import ast
import builtins
import concurrent.futures
import gzip
import importlib.abc
import importlib.machinery
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

import bool_values
import configmap
from config_sites import index
from generate_config import Instrument, Profile
from generate_map import PINNED, TracedPool
from identity_facts import _CLASS_DICT, _MRO, _slot, type_assign_version, type_get_version

QUALNAME = type.__dict__["__qualname__"]


def flag_leaf(node):
    while isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    if not isinstance(node, ast.Attribute):
        return None
    owner = node.value
    if ((isinstance(owner, ast.Name) and owner.id == "self") or
            (isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "self")):
        return node
    return None


def guards(path, attr, lines, tree, parents, booleans):
    uses = [n for n in ast.walk(tree) if isinstance(n, (ast.Attribute, ast.Name)) and
            getattr(n, "attr", getattr(n, "id", None)) == attr and isinstance(n.ctx, ast.Load) and n.lineno in lines]
    if not uses or any(isinstance(n, ast.Name) for n in uses):
        # This profiler observes attributes. A bare load cannot be attributed
        # to the runtime class that supplied an inherited flag.
        return {}
    by_line = defaultdict(list)
    for use in uses:
        parent, found = parents.get(use), None
        while isinstance(parent, (ast.UnaryOp, ast.BoolOp)):
            if isinstance(parent, ast.BoolOp):
                leaves = []
                shape = bool_values.tree(parent, leaves)
                matching = [i for i, leaf in enumerate(leaves) if flag_leaf(leaf) is use]
                aliases = [n for n in ast.walk(parent) if isinstance(n, ast.Attribute) and n.attr == attr]
                site = bool_values.location(path, parent)
                if len(matching) == len(aliases) == 1 and site in booleans:
                    found = [site, shape, matching[0]]
            parent = parents.get(parent)
        by_line[use.lineno].append(found)
    # f_lineno cannot distinguish separate loads on the same line.
    return {n: group[0] if len(group) == 1 else None for n, group in by_line.items()}


def definitions(root):
    approved, _ = index()
    booleans = bool_values.profile()["sites"]
    trees = {}
    rows = {}
    for symbol, (path, lines) in approved.items():
        if path not in trees:
            tree = ast.parse((root / path).read_bytes())
            trees[path] = tree, {child: n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}
        key = path, tuple(lines), symbol.rsplit(".", 1)[1]
        if key not in rows:
            rows[key] = guards(path, key[2], lines, *trees[path], booleans)
    result = {}
    for f in (root / "sqlglot").rglob("*.py"):
        module = configmap.module_name(f.relative_to(root).as_posix())
        if not any(s.startswith(module + ".") for s in approved):
            continue
        for symbol, definition in configmap.members(ast.parse(f.read_bytes()), module):
            if symbol not in approved:
                continue
            val = configmap.literal(definition.value)
            path, lines = approved[symbol]
            by_line = rows[(path, tuple(lines), symbol.rsplit(".", 1)[1])]
            if type(val) is bool and any(by_line.values()):
                result[symbol] = path, by_line, val
    return result


class FlagReads(Profile):
    def __init__(self, root, tests, defs):
        super().__init__(root, tests)
        self.defs = defs
        self.scalars = set(defs)
        self.attr_names = {s.rsplit(".", 1)[1] for s in defs}
        self.rows = defaultdict(dict)
        self.opaque = defaultdict(int)
        self.external = 0
        self.loading = 0
        self.owners = {}
        self.by_attr = defaultdict(set)
        for symbol in defs:
            self.by_attr[symbol.rsplit(".", 1)[1]].add(symbol)

    def activate(self, mask):
        self.active = mask

    def audit(self, event, args):
        if event == "subprocess.Popen":
            self.external |= self.active

    def receiver(self, cls, attr):
        key = id(cls), attr
        tag = type_get_version(cls) if type_get_version else 0
        saved = self.owners.get(key)
        if tag and saved and saved[0] == tag:
            return saved[1:]
        symbol, ordinary, found = None, True, False
        for base in _MRO.__get__(cls):
            namespace = _CLASS_DICT.__get__(base)
            if any(type(k) is not str for k in namespace):
                ordinary = False
                break
            if attr not in namespace:
                continue
            found = True
            module, qual = namespace.get("__module__"), QUALNAME.__get__(base)
            if type(module) is str and type(qual) is str:
                name = ".".join((module, qual, attr))
                if name in self.defs:
                    symbol = name
            # A descriptor on an overriding class can dispatch to the flag.
            if type(namespace[attr]) is not bool:
                ordinary = False
            break
        ordinary = ordinary and found
        if ordinary:
            bases = _MRO.__get__(cls)
            ordinary = (all(all(type(k) is str for k in _CLASS_DICT.__get__(base)) for base in bases)
                        and _slot(cls, "__getattribute__") is object.__getattribute__)
        if type_assign_version and not tag:
            type_assign_version(cls)
            tag = type_get_version(cls)
        if tag:
            self.owners[key] = tag, symbol, ordinary
        return symbol, ordinary

    def read_attr(self, obj, attr, *default):
        value = getattr(obj, attr, *default)
        if type(attr) is not str or attr not in self.attr_names:
            return value
        cls = type(obj)
        is_class = any(base is type for base in _MRO.__get__(cls))
        symbol, ordinary = self.receiver(obj if is_class else cls, attr)
        if not ordinary or is_class:
            for affected in self.by_attr[attr]:
                if self.loading or not self.active:
                    self.unsafe.add(affected)
                else:
                    self.opaque[affected] |= self.active
            return value
        if symbol is None:
            return value
        path, lines, expected = self.defs[symbol]
        frame = sys._getframe(1)
        line = frame.f_lineno
        if (not self.active or self.loading or self.relative(frame.f_code.co_filename) != path or line not in lines):
            self.unsafe.add(symbol)
        elif value is not expected:
            self.opaque[symbol] |= self.active
        else:
            rows = self.rows[symbol]
            rows[line] = rows.get(line, 0) | self.active
        return value

    def save(self, output):
        pool, symbols = {0: 0}, {}
        for symbol, (path, guards, value) in sorted(self.defs.items()):
            if symbol in self.unsafe or not self.rows[symbol]:
                continue
            rows = [[line, guards[line], pool.setdefault(mask, len(pool))]
                    for line, mask in sorted(self.rows[symbol].items())]
            symbols[symbol] = [value, pool.setdefault(self.opaque[symbol], len(pool)), rows]
        incomplete = set(self.tests) - (self.passed - self.failed)
        incomplete.update(t for t, bit in self.bits.items() if bit & self.external)
        d = {"tests": self.tests, "incomplete": sorted(incomplete), "symbols": symbols,
             "pool": [format(mask, "x") for mask in pool]}
        with output.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(d, separators=(",", ":")).encode())
        print(f"{len(symbols)} bounded flags, {len(incomplete)} incomplete tests")


def main():
    root, inventory, output = [Path(a).resolve() for a in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Expected the pinned checkout")
    subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"], check=True)
    defs = definitions(root)
    tests = sorted(t["id"] for t in json.loads(inventory.read_bytes())["tests"])
    rec = FlagReads(root, tests, defs)

    class Loader(importlib.abc.Loader):
        def __init__(self, origin):
            self.origin = origin
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            tree = Instrument(rec).visit(ast.parse(Path(self.origin).read_bytes(), self.origin))
            rec.loading += 1
            try:
                exec(compile(ast.fix_missing_locations(tree), self.origin, "exec"), module.__dict__)
            finally:
                rec.loading -= 1

    class Finder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".", 1)[0] in {"sqlglot", "tests"}:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and spec.origin and spec.origin.endswith(".py"):
                    spec.loader = Loader(spec.origin)
                return spec
            return None

    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    builtins._br_read = rec.read_attr
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.addaudithook(rec.audit)
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())
    result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[rec])
    if result not in (0, 1):
        raise SystemExit(result)
    temp = output.with_suffix(".tmp")
    rec.save(temp)
    temp.replace(output)


if __name__ == "__main__":
    main()
