"""Measure test, operand MRO, and class namespace for ordinary isinstance calls."""

import ast
import builtins
import concurrent.futures
import gzip
import importlib.abc
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from class_checks import BUILTINS, location, target
from generate_map import PINNED, Recorder, TracedPool
from identity_facts import _CLASS_DICT, _MRO, _slot, type_assign_version, type_get_version
from observe_base import ProbeFinder

try:
    from _testcapi import dict_get_version
except ImportError:
    dict_get_version = None

NATIVE_CHECK = isinstance
KNOWN = {name: vars(builtins)[name] for name in BUILTINS}
SAFE_READERS = {id(t.__getattribute__) for t in (*KNOWN.values(), type(None))}
OBJECT_CLASS = vars(object)["__class__"]
MODULE_DICT = vars(types.ModuleType)["__dict__"]


def declared(tree):
    bind = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bind.add(n.id)
        elif isinstance(n, ast.arg):
            bind.add(n.arg)
        elif isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.ExceptHandler,
                            ast.MatchAs, ast.MatchStar)) and n.name:
            bind.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bind.add(n.rest)
        elif isinstance(n, ast.alias):
            bind.add(n.asname or n.name.split(".")[0])
    return frozenset(bind & (BUILTINS | {"exp"}))


class ClassRecorder(Recorder):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.sites = {}
        self.external = 0
        self.classes = {}
        self.type_cache = {}
        self.mros = {}
        self.expression_cache = {}
        self.namespace_cache = {}
        self.context_ids = {}
        self.contexts = []
        self.loading = 0
        self.key_safety = {}

    def activate(self, mask):
        self.active = mask

    def audit(self, event, args):
        if event == "subprocess.Popen":
            self.external |= self.active

    def class_id(self, cls):
        label = id(cls)
        if label not in self.classes:
            self.classes[label] = (cls, len(self.classes))
        return self.classes[label][1]

    def mro(self, cls):
        tag = type_get_version(cls) if type_get_version else 0
        cached = self.type_cache.get(id(cls))
        if tag and cached and cached[0] == tag:
            return cached[1]
        idx = None
        # isinstance can read value.__class__ when the actual MRO fails.
        # A property, proxy or custom __getattribute__ can change its answer
        # or introduce a side effect even when the type relationship is known.
        bases = _MRO.__get__(cls)
        if (type(cls) is type and all(all(type(k) is str for k in _CLASS_DICT.__get__(t)) for t in bases)
                and _slot(cls, "__class__") is OBJECT_CLASS and id(_slot(cls, "__getattribute__")) in SAFE_READERS):
            parts = tuple(self.class_id(base) for base in _MRO.__get__(cls))
            idx = self.mros.setdefault(parts, len(self.mros))
        if type_assign_version and not tag:
            type_assign_version(cls)
            tag = type_get_version(cls)
        if tag:
            self.type_cache[id(cls)] = (tag, idx)
        return idx

    def expressions(self, namespace):
        if namespace is None:
            return ()
        version = dict_get_version(namespace) if dict_get_version else None
        if version is not None and version in self.expression_cache:
            return self.expression_cache[version]
        pairs = []
        for name, cls in namespace.items():
            if type(name) is str and not name.startswith("_") and type(cls) is type:
                cls_dict = _CLASS_DICT.__get__(cls)
                if any(type(k) is not str for k in cls_dict):
                    continue
                owner = cls_dict.get("__module__")
                if type(owner) is str and owner.startswith("sqlglot."):
                    pairs.append(("exp." + name, self.class_id(cls)))
        values = tuple(sorted(pairs))
        if version is not None:
            self.expression_cache[version] = values
        return values

    def string_keys(self, namespace):
        # An injected key with a colliding hash can run arbitrary equality.
        if type(namespace) is not dict:
            return False
        version = dict_get_version(namespace) if dict_get_version else None
        if version is not None and version in self.key_safety:
            return self.key_safety[version]
        safe = all(type(k) is str for k in namespace)
        if version is not None:
            self.key_safety[version] = safe
        return safe

    def context(self, frame, blocked):
        g, b = frame.f_globals, frame.f_builtins
        if not all(self.string_keys(ns) for ns in (g, b, sys.modules)):
            return None
        exp = None if "exp" in blocked else g.get("exp", b.get("exp"))
        module = sys.modules.get("sqlglot.expressions")
        ns = MODULE_DICT.__get__(exp) if exp is module and type(exp) is types.ModuleType else None
        if ns is not None and not self.string_keys(ns):
            return None
        cache_key = None
        if dict_get_version:
            cache_key = (dict_get_version(g), dict_get_version(b),
                         dict_get_version(ns) if ns is not None else None, blocked)
        if cache_key is not None and cache_key in self.namespace_cache:
            return self.namespace_cache[cache_key]
        namespace = self.expressions(ns)
        bindings = tuple(sorted((n, self.class_id(t)) for n, t in KNOWN.items()
                                if n not in blocked and g.get(n, b.get(n)) is t))
        key = bindings, namespace
        if key not in self.context_ids:
            self.context_ids[key] = len(self.contexts)
            self.contexts.append(dict((*bindings, *namespace)))
        index = self.context_ids[key]
        if cache_key is not None:
            self.namespace_cache[cache_key] = index
        return index

    def probe(self, path, name, blocked):
        state = self.sites.setdefault(path, {}).setdefault(name, {"opaque": 0, "values": {}, "startup": False})
        def callee(fn):
            if fn is not NATIVE_CHECK:
                state["opaque"] |= self.active
            return fn

        def observe(value):
            active = self.active
            if not active or self.loading:
                state["startup"] = True
            elif not state["startup"] and active & ~state["opaque"]:
                frame = sys._getframe(1)
                mro = self.mro(type(value)) if frame.f_code.co_flags & 1 else None
                ctx = self.context(frame, blocked) if mro is not None else None
                key = ctx, mro
                entries = state["values"]
                if ctx is None or (key not in entries and len(entries) >= 1024):
                    state["opaque"] |= active
                else:
                    entries[key] = entries.get(key, 0) | active
            return value
        return callee, observe

    def save(self, target):
        pool, sites = {0: 0}, {}
        for path, records in sorted(self.sites.items()):
            by_location = {}
            for name, state in sorted(records.items()):
                if state["startup"]:
                    continue
                opaque = state["opaque"]
                values = [[ctx, idx, pool.setdefault(mask & ~opaque, len(pool))]
                          for (ctx, idx), mask in sorted(state["values"].items()) if mask & ~opaque]
                by_location[name] = [pool.setdefault(opaque, len(pool)), values]
            if by_location:
                sites[path] = by_location
        incomplete = set(self.tests) - (self.passed - self.failed)
        incomplete.update(t for t, bit in self.bits.items() if bit & self.external)
        data = {"tests": self.tests, "sites": sites, "pool": [format(v, "x") for v in pool],
                "contexts": self.contexts, "mros": list(self.mros), "incomplete": sorted(incomplete)}
        with target.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(data, separators=(",", ":")).encode())
        print(f"{sum(map(len, sites.values()))} class-check sites, {len(self.mros)} MROs, {len(incomplete)} incomplete tests")


class Transform(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.recorder = recorder
        self.path = path
        self.probes = []
        self.blocked = None

    def visit_FunctionDef(self, node):
        prior = self.blocked
        self.blocked = (prior or frozenset()) | declared(node)
        node.body = [self.visit(n) for n in node.body]
        self.blocked = prior
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        prior = self.blocked
        self.blocked = (prior or frozenset()) | declared(node)
        node.body = self.visit(node.body)
        self.blocked = prior
        return node

    def visit_ClassDef(self, node):
        # Methods can capture enclosing functions but cannot capture class
        # bindings by these public names. Omit executable class-body code.
        for n in node.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(n)
        return node

    def visit_Call(self, node):
        shape = target(node) if self.blocked is not None else None
        self.generic_visit(node)
        if shape is not None:
            offset = len(self.probes)
            self.probes.extend(self.recorder.probe(self.path, location(node), self.blocked))
            def wrap(expr, index):
                fn = ast.Subscript(ast.Name("_br_class_checks", ast.Load()), ast.Constant(index), ast.Load())
                return ast.copy_location(ast.Call(fn, [expr], []), expr)
            # Evaluate and return the original callable once, in its original
            # order. A shadowed function is still called by the original frame.
            node.func = wrap(node.func, offset)
            value = node.args[0]
            node.args[0] = wrap(value, offset + 1)
        return node


class Loader(importlib.abc.Loader):
    def __init__(self, origin, recorder):
        self.origin, self.recorder = origin, recorder

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        transform = Transform(self.recorder, self.recorder.relative(self.origin))
        tree = transform.visit(ast.parse(Path(self.origin).read_bytes(), self.origin))
        module._br_class_checks = transform.probes
        self.recorder.loading += 1
        try:
            exec(compile(ast.fix_missing_locations(tree), self.origin, "exec"), module.__dict__)
        finally:
            self.recorder.loading -= 1


if __name__ == "__main__":
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    revision = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if revision != PINNED or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected clean pinned checkout")
    tests = sorted(t["id"] for t in json.loads(inventory.read_bytes())["tests"])
    recorder = ClassRecorder(root, tests)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    sys.path.insert(0, str(root))
    sys.addaudithook(recorder.audit)
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.meta_path.insert(0, ProbeFinder(recorder, Loader))
    result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[recorder])
    if result not in (0, 1):
        raise SystemExit(result)
    tmp = output.with_suffix(".tmp")
    recorder.save(tmp)
    tmp.replace(output)
