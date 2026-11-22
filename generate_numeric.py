"""Collect integer branch-stability intervals for module-local scalar constants.

A scope is eligible only if every syntactically reachable use is a supported
integer operation, a local assignment, a default argument, or a known local
parameter binding. Unexpected operand types force that test to remain selected.
"""

import ast
import builtins
import concurrent.futures
import functools
import gzip
import importlib.machinery
import json
import operator
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from configmap import literal, members, module_members, module_name
from deps import Dependencies
from generate_map import PINNED, Recorder, TracedPool


def var(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def analyze_module(path, tree, definitions, external):
    parents = {
        item: node for node in ast.walk(tree) for item in ast.iter_child_nodes(node)
    }
    reads = defaultdict(list)
    fields = set()
    foreign_decisions = set()
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            constructor = next(
                (
                    f
                    for f in n.body
                    if isinstance(f, ast.FunctionDef) and f.name == "__init__"
                ),
                None,
            )
            if constructor:
                functions[n.name] = constructor
        if isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Load):
            reads[var(n)].append(n)
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            fields.add(n.attr)
        if isinstance(n, ast.AugAssign) and not isinstance(
            n.op, (ast.Add, ast.Sub, ast.Mult)
        ):
            foreign_decisions.add(var(n.target))

    def destination(node):
        parent = parents.get(node)
        if isinstance(parent, ast.arguments):
            args = [*parent.posonlyargs, *parent.args]
            for v, a in [
                *zip(parent.defaults, args[len(args) - len(parent.defaults) :]),
                *zip(parent.kw_defaults, parent.kwonlyargs),
            ]:
                if v is node:
                    return [a.arg]
        if isinstance(parent, ast.Compare):
            if all(
                isinstance(op, (ast.Lt, ast.LtE, ast.Eq, ast.NotEq, ast.Gt, ast.GtE))
                for op in parent.ops
            ):
                return []
            if len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.Is, ast.IsNot)):
                peer = (
                    parent.left
                    if parent.comparators[0] is node
                    else parent.comparators[0]
                )
                if isinstance(peer, ast.Constant) and peer.value is None:
                    return []
        if isinstance(parent, ast.UnaryOp):
            if isinstance(parent.op, ast.Not):
                return []
            if isinstance(parent.op, (ast.UAdd, ast.USub)):
                return destination(parent)
        if isinstance(parent, ast.BinOp) and isinstance(
            parent.op, (ast.Add, ast.Sub, ast.Mult)
        ):
            return destination(parent)
        if isinstance(parent, (ast.If, ast.While, ast.IfExp)) and parent.test is node:
            return []
        if (
            isinstance(parent, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and parent.value is node
        ):
            targets = (
                parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            )
            return [var(t) for t in targets]
        arg = node
        if isinstance(parent, ast.keyword):
            arg = parent
            parent = parents[parent]
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
            name = parent.func.id
            if name in {"min", "max"} and not parent.keywords and len(parent.args) >= 2:
                return destination(parent)
            if name in functions:
                fn = functions[name]
                params = [*fn.args.posonlyargs, *fn.args.args]
                if fn.name == "__init__":
                    params = params[1:]
                if isinstance(arg, ast.keyword):
                    return (
                        [arg.arg]
                        if arg.arg in {p.arg for p in params + fn.args.kwonlyargs}
                        else [None]
                    )
                if arg in parent.args:
                    i = parent.args.index(arg)
                    if i < len(params):
                        return [params[i].arg]
        return [None]

    eligible = {}
    for symbol, stmt in definitions:
        name = symbol.split(".")[-1]
        attrs = {name}
        pending = [name]
        while pending:
            current = pending.pop()
            if (
                current is None
                or current in foreign_decisions
                or (current == name and external.get(current, set()) - {path})
                or (current in fields and external.get("." + current, set()) - {path})
            ):
                break
            for ref in reads[current]:
                for target in destination(ref):
                    if target not in attrs:
                        attrs.add(target)
                        pending.append(target)
        else:
            if reads[name]:
                eligible[symbol] = stmt
    return eligible


def discover(root):
    external = defaultdict(set)
    candidate_files = {}
    for directory in ("sqlglot", "tests"):
        for p in (root / directory).rglob("*.py"):
            tree = ast.parse(p.read_bytes())
            path = p.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
                    node.ctx, ast.Load
                ):
                    external[var(node)].add(path)
                    if isinstance(node, ast.Attribute):
                        external["." + node.attr].add(path)
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        external[alias.name].add(path)
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.isidentifier()
                ):
                    external[node.value].add(path)
            if directory == "sqlglot":
                mod = module_name(path)
                defs = [
                    (sym, st)
                    for sym, st in [*members(tree, mod), *module_members(tree, mod)]
                    if sym.split(".")[-1].isupper() and type(literal(st.value)) is int
                ]
                if defs:
                    candidate_files[path] = (tree, defs)
    return {
        path: {
            sym: literal(st.value)
            for sym, st in analyze_module(path, tree, defs, external).items()
        }
        for path, (tree, defs) in candidate_files.items()
    }


def interval(value, slope, op, truth):
    """Closed integer delta interval around zero that preserves a comparison."""
    if not slope:
        return None, None
    if not truth:
        op = {"lt": "ge", "le": "gt", "gt": "le", "ge": "lt", "eq": "ne", "ne": "eq"}[
            op
        ]
    if op == "eq":
        return 0, 0
    if op == "ne":
        q, r = divmod(-value, slope)
        if r:
            return None, None
        return (q + 1, None) if q < 0 else (None, q - 1)
    if op in {"gt", "ge"}:
        value, slope, op = -value, -slope, {"gt": "lt", "ge": "le"}[op]
    limit = (-1 if op == "lt" else 0) - value
    if slope > 0:
        return None, limit // slope
    return -(-limit // slope), None


class Affine(int):
    def __new__(cls, n, terms):
        obj = int.__new__(cls, n)
        obj.terms = terms
        return obj

    @staticmethod
    def parts(value):
        return int(value), value.terms if isinstance(value, Affine) else {}

    def operate(self, other, op, reverse=False):
        fn = getattr(operator, op)
        if type(other) is not int and not isinstance(other, Affine):
            for s in self.terms:
                profile.bound(s, 0, 0)
            return fn(other, int(self)) if reverse else fn(int(self), other)
        a, ta = self.parts(self)
        b, tb = self.parts(other)
        if reverse:
            a, ta, b, tb = b, tb, a, ta
        result = fn(a, b)
        terms = {}
        for s in ta.keys() | tb.keys():
            ca, cb = ta.get(s, 0), tb.get(s, 0)
            if op == "mul":
                if ca and cb:
                    profile.bound(s, 0, 0)
                factor = ca * b + cb * a
            elif op in {"add", "sub"}:
                factor = ca + cb if op == "add" else ca - cb
            else:
                lo, hi = interval(a - b, ca - cb, op, result)
                profile.bound(s, lo, hi)
                continue
            if factor:
                terms[s] = factor
        return Affine(result, terms) if terms else result

    def __neg__(self):
        return self.operate(-1, "mul")

    def __pos__(self):
        return self

    def __bool__(self):
        return self.operate(0, "ne")


for name in ("add", "sub", "mul", "lt", "le", "eq", "ne", "gt", "ge"):
    setattr(
        Affine, "__" + name + "__", functools.partialmethod(Affine.operate, op=name)
    )
    if name in {"add", "sub", "mul"}:
        setattr(
            Affine,
            "__r" + name + "__",
            functools.partialmethod(Affine.operate, op=name, reverse=True),
        )


class Profile(Recorder):
    def __init__(self, root, nodes):
        super().__init__(root, nodes)
        self.intervals = defaultdict(dict)
        self.current = {}
        self.unsafe = set()

    def activate(self, mask):
        while self.active:
            bit = self.active & -self.active
            self.active ^= bit
            for s, (lo, hi) in self.current.items():
                vals = self.intervals[s].setdefault(bit.bit_length() - 1, [None, None])
                tighten(vals, lo, hi)
        self.current.clear()
        self.active = mask

    def bound(self, s, lo, hi):
        if lo is None and hi is None:
            return
        if not self.active:
            self.unsafe.add(s)
            return
        tighten(self.current.setdefault(s, [None, None]), lo, hi)


def tighten(vals, lo, hi):
    if lo is not None:
        vals[0] = max(lo, vals[0]) if vals[0] is not None else lo
    if hi is not None:
        vals[1] = min(hi, vals[1]) if vals[1] is not None else hi


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        symbols = eligible[Path(self.path).relative_to(root).as_posix()]
        for sym, stmt in [*members(tree, fullname), *module_members(tree, fullname)]:
            if sym in symbols:
                stmt.value = ast.copy_location(
                    ast.Call(
                        ast.Name("_br_affine", ast.Load()),
                        [stmt.value, ast.Dict([ast.Constant(sym)], [ast.Constant(1)])],
                        [],
                    ),
                    stmt.value,
                )
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in modules:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin:
                spec.loader = Loader(fullname, spec.origin)
            return spec
        return None


def main():
    global profile, root, eligible, modules
    root, inventory, output = [Path(p).resolve() for p in sys.argv[1:]]
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PINNED
        or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode
    ):
        raise ValueError("Expected pinned base")
    eligible = {p: s for p, s in discover(root).items() if s}
    modules = {module_name(p) for p in eligible}
    print("Eligible", eligible, flush=True)
    deps = Dependencies(output.parent / "deps.json.gz")
    profile = Profile(
        root, sorted(r["id"] for r in json.loads(inventory.read_bytes())["tests"])
    )
    profile.only = set().union(*(deps.file(p) for p in eligible))
    builtins._br_affine = Affine
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.meta_path.insert(0, Finder())
    rc = pytest.main(
        ["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[profile]
    )
    if rc not in (0, 1):
        raise SystemExit(rc)
    incomplete = profile.only - (profile.passed - profile.failed)
    result = {
        "tests": profile.tests,
        "incomplete": sorted(incomplete),
        "symbols": {},
        "paths": eligible,
    }
    for p, syms in eligible.items():
        for symbol, value in syms.items():
            if symbol not in profile.unsafe:
                result["symbols"][symbol] = {
                    "base": value,
                    "bounds": profile.intervals[symbol],
                }
    with (
        open(output, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
    ):
        gz.write(json.dumps(result, separators=(",", ":")).encode())
    print(
        "Profiled",
        len(result["symbols"]),
        "symbols;",
        len(incomplete),
        "incomplete tests",
    )


if __name__ == "__main__":
    main()
