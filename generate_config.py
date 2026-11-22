"""Profile class attributes, dictionary entries, and registry callbacks on the base.

Run with the same arguments and dependencies as generate_map.py. Only in-memory
compilation is instrumented; neither the checkout nor its bytecode is modified.
"""
import ast
import builtins
import concurrent.futures
import functools
import gzip
import importlib.machinery
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from configmap import literal, members, module_name
from generate_map import PINNED, Recorder, TracedPool


class Profile(Recorder):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.seen = set()
        self.masks = {}
        self.unsafe = set()
        self.scalars = set()
        self.attr_names = {"arg_types", "required_args"}
        self.factories = set()
        self.read_dictionaries = set()

    def activate(self, mask):
        for name in self.seen:
            self.masks[name] = self.masks.get(name, 0) | self.active
        self.seen.clear()
        self.active = mask

    def record(self, name):
        if self.active:
            self.seen.add(name)
        else:
            self.unsafe.add(name)

    def scan(self, root):
        for f in (root / "sqlglot").rglob("*.py"):
            module = module_name(f.relative_to(root).as_posix())
            tree = ast.parse(f.read_bytes())
            for name, stmt in members(tree, module):
                value = literal(stmt.value)
                attr = name.split(".")[-1]
                if attr == "arg_types" or (
                    attr.isupper() and type(value) in (int, str, bool, float, bytes)
                ):
                    self.attr_names.add(attr)
                    self.scalars.add(name)
            for fn in tree.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                if not body or not isinstance(body[-1], ast.Return):
                    continue
                deferred = body[-1].value
                if len(body) == 1 and isinstance(deferred, ast.Lambda):
                    self.factories.add(module + "." + fn.name)
                if (len(body) == 2 and isinstance(body[0], ast.FunctionDef)
                        and not body[0].decorator_list and not body[0].args.defaults
                        and isinstance(deferred, ast.Name) and deferred.id == body[0].name):
                    self.factories.add(module + "." + fn.name)

    @functools.lru_cache(None)
    def owner(self, cls, attr):
        for owner in cls.__mro__:
            if attr in vars(owner):
                name = owner.__module__ + "." + owner.__qualname__ + "." + attr
                return name if name in self.scalars else None
        return None

    def read_attr(self, obj, attr, *default):
        value = getattr(obj, attr, *default)
        if attr not in self.attr_names:
            return value
        cls = obj if isinstance(obj, type) else type(obj)
        source_attr = "arg_types" if attr == "required_args" else attr
        name = self.owner(cls, source_attr)
        if name:
            # __init_subclass__ materializes required_args, handled by the
            # corresponding read instrumentation above.
            if source_attr != "arg_types" or sys._getframe(1).f_code.co_qualname != "Expr.__init_subclass__":
                self.record(name)
        return value

    def callback(self, symbol, factory, *args, **kwargs):
        func = factory(*args, **kwargs)
        name = getattr(factory, "__module__", "") + "." + getattr(factory, "__qualname__", "")
        if name not in self.factories or not isinstance(func, types.FunctionType):
            return func
        self.masks.setdefault(symbol, 0)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            self.record(symbol)
            return func(*args, **kwargs)
        return wrapped

    def dictionary(self, name, entries):
        self.read_dictionaries.add(name)
        for key in entries:
            self.masks.setdefault(name + ":" + key, 0)
        return ValueMap(self, name, entries)


class ValueMap(dict):
    """Value-only mutations preserve length, membership, keys and iteration."""
    def __init__(self, profile, name, entries):
        super().__init__(entries)
        self.profile = profile
        self.name = name

    def _read(self, key):
        if isinstance(key, str) and key in self:
            self.profile.record(self.name + ":" + key)

    def get(self, key, *args):
        self._read(key)
        return super().get(key, *args)

    def __getitem__(self, key):
        self._read(key)
        return super().__getitem__(key)

    def _bulk(self):
        self.profile.record(self.name)

    def items(self):
        self._bulk()
        return super().items()

    def values(self):
        self._bulk()
        return super().values()

    def __iter__(self):
        return super().__iter__()

    def copy(self):
        self.profile.unsafe.add(self.name)
        return super().copy()

    def __or__(self, other):
        self.profile.unsafe.add(self.name)
        return super().__or__(other)

    def __ror__(self, other):
        self.profile.unsafe.add(self.name)
        return super().__ror__(other)


class Instrument(ast.NodeTransformer):
    def __init__(self, profile):
        self.profile = profile

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.profile.attr_names:
            return ast.copy_location(ast.Call(ast.Name("_br_read", ast.Load()),
                                              [node.value, ast.Constant(node.attr)], []), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            node.func.id = "_br_read"
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        if fullname.startswith("sqlglot"):
            for symbol, stmt in members(tree, fullname):
                node = stmt.value
                if symbol in profile.scalars:
                    profile.masks.setdefault(symbol, 0)
                if not isinstance(node, ast.Dict) or symbol.endswith(".arg_types"):
                    continue
                if (node.keys and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys)
                        and all(literal(v) is not ... for v in node.values)):
                    stmt.value = ast.copy_location(ast.Call(ast.Name("_br_dict", ast.Load()),
                                                    [ast.Constant(symbol), node], []), node)
                else:
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(value, ast.Call):
                            value.args[:0] = [ast.Constant(symbol + ":" + key.value), value.func]
                            value.func = ast.Name("_br_callback", ast.Load())
        tree = Instrument(profile).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"sqlglot", "tests"}:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec
        return None


def main():
    global profile
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Unexpected base revision")
    if subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Dirty base checkout")
    nodes = sorted(r["id"] for r in json.loads(inventory.read_text())["tests"])
    profile = Profile(root, nodes)
    profile.scan(root)
    builtins._br_read = profile.read_attr
    builtins._br_callback = profile.callback
    builtins._br_dict = profile.dictionary
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())
    result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[profile])
    if result not in (0, 1):
        raise SystemExit(result)
    incomplete = set(nodes) - (profile.passed - profile.failed)
    out = {}
    for token, mask in profile.masks.items():
        symbol = token.split(":")[0]
        if token in profile.unsafe or symbol in profile.unsafe:
            continue
        if ":" in token and symbol in profile.read_dictionaries:
            mask |= profile.masks.get(symbol, 0)
        out[token] = hex(mask)
    print("Incomplete", len(incomplete), "class dependencies", len(out))
    with open(output, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps({"tests": nodes, "masks": out, "incomplete": sorted(incomplete)}, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()
