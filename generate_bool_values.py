"""Observe pure operand truth values without executing skipped Python expressions."""

import ast
import concurrent.futures
import gzip
import importlib.abc
import json
import operator
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from bool_values import location, tree
from generate_map import PINNED, Recorder, TracedPool
from observe_base import ProbeFinder
from slot_facts import SlotFacts


UNKNOWN = object()
SLOTS = SlotFacts()
SCALARS = frozenset((type(None), bool, int, float, str, bytes))
LEN_TYPES = frozenset((list, tuple, dict, str, bytes))
SIZED = frozenset((list, tuple, dict, str, bytes, set, frozenset, range))
TRUTH_TYPES = SCALARS | SIZED
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
       ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Is: operator.is_, ast.IsNot: operator.is_not}
NATIVE_LEN = len


def exact(value, allowed_types):
    cls = type(value)
    return type(cls) is type and cls in allowed_types


def slot_value(value):
    cls = type(value)
    if type(cls) is type and cls in TRUTH_TYPES:
        return value
    return value if type(cls) is type and cls in SLOTS.classes else UNKNOWN


@lru_cache(maxsize=None)
def scope_names(code):
    return frozenset((*code.co_varnames, *code.co_cellvars, *code.co_freevars))


def truth(value):
    if value is UNKNOWN:
        return UNKNOWN
    cls = type(value)
    if type(cls) is type and cls in TRUTH_TYPES:
        return bool(value)
    # Python's default truth value is true in the absence of both slots.
    # Inspect the class namespaces without fetching or calling any descriptors.
    return True if SLOTS.default_truth(cls) else UNKNOWN


def compile_operand(n):
    if isinstance(n, ast.Attribute) and n.attr in SLOTS.names:
        owner = compile_operand(n.value)
        if owner:
            return lambda local, builtin_len: slot_value(SLOTS.read(owner(local, builtin_len), n.attr, UNKNOWN))
    if isinstance(n, ast.Name):
        return lambda local, builtin_len: local.get(n.id, UNKNOWN)
    if isinstance(n, ast.Constant) and exact(n.value, SCALARS):
        return lambda local, builtin_len: n.value
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        operand = compile_operand(n.operand)
        if operand:
            def negate(local, builtin_len):
                v = truth(operand(local, builtin_len))
                return UNKNOWN if v is UNKNOWN else not v
            return negate
    if isinstance(n, ast.Call) and not n.keywords:
        if isinstance(n.func, ast.Name) and n.func.id == "len" and len(n.args) == 1:
            operand = compile_operand(n.args[0])
            if operand:
                def length(local, builtin_len):
                    v = operand(local, builtin_len)
                    return NATIVE_LEN(v) if builtin_len and exact(v, LEN_TYPES) else UNKNOWN
                return length
        if isinstance(n.func, ast.Attribute) and n.func.attr == "get" and 1 <= len(n.args) <= 2:
            container = compile_operand(n.func.value)
            args = [compile_operand(x) for x in n.args]
            if container and all(args):
                from_slots = any(isinstance(part, ast.Attribute) and part.attr in SLOTS.names for part in ast.walk(n))
                def lookup(local, builtin_len):
                    d = container(local, builtin_len)
                    if type(d) is not dict or len(d) > 32:
                        return UNKNOWN
                    key = args[0](local, builtin_len)
                    default = args[1](local, builtin_len) if len(args) == 2 else None
                    if default is UNKNOWN or not exact(key, SCALARS):
                        return UNKNOWN
                    # Custom keys could run __eq__ on a hash collision.
                    if any(not exact(k, SCALARS) for k in d):
                        return UNKNOWN
                    value = dict.get(d, key, default)
                    return slot_value(value) if from_slots else value
                return lookup
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in CMP:
        left, right = compile_operand(n.left), compile_operand(n.comparators[0])
        op = type(n.ops[0])
        if op in (ast.Is, ast.IsNot):
            pair = n.left, n.comparators[0]
            # Recomputing len or fetching the source AST's literal can
            # materialize a different object with the same primitive value.
            singleton = any(isinstance(x, ast.Constant) and (x.value is None or type(x.value) is bool) for x in pair)
            if not singleton and not all(isinstance(x, ast.Name) for x in pair):
                return None
        if left and right:
            def compare(local, builtin_len):
                a, b = left(local, builtin_len), right(local, builtin_len)
                if a is UNKNOWN or b is UNKNOWN:
                    return UNKNOWN
                if op in (ast.Is, ast.IsNot):
                    return CMP[op](a, b)
                if exact(a, SCALARS) and exact(b, SCALARS):
                    try:
                        return CMP[op](a, b)
                    except TypeError:
                        pass
                return UNKNOWN
            return compare
    return None


class BoolRecorder(Recorder):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.sites = {}
        self.external = 0

    def activate(self, mask):
        self.active = mask

    def audit(self, event, args):
        if event == "subprocess.Popen":
            self.external |= self.active

    def probe(self, name, readers, variables):
        state = self.sites.setdefault(name, {"values": {}, "opaque": 0, "startup": False})
        def observe():
            active = self.active
            if not active:
                state["startup"] = True
            elif not state["startup"] and active & ~state["opaque"]:
                frame = sys._getframe(1)
                if not frame.f_code.co_flags & 1:  # CO_OPTIMIZED, real function locals
                    state["opaque"] |= active
                    return True
                scope = scope_names(frame.f_code)
                actual = frame.f_locals
                # A key manually inserted into f_locals cannot shadow a
                # LOAD_GLOBAL instruction. Read only compiler-declared locals.
                local = actual if variables <= scope else {k: actual.get(k, UNKNOWN) for k in variables & scope}
                if "len" in scope:
                    builtin_len = actual.get("len", UNKNOWN) is NATIVE_LEN
                elif type(frame.f_globals) is dict and type(frame.f_builtins) is dict:
                    builtin_len = frame.f_globals.get("len", frame.f_builtins.get("len")) is NATIVE_LEN
                else:
                    builtin_len = False
                vector = []
                for read in readers:
                    v = truth(read(local, builtin_len)) if read is not None else UNKNOWN
                    vector.append("?" if v is UNKNOWN else "T" if v else "F")
                key = "".join(vector)
                values = state["values"]
                if key not in values and len(values) >= 64:
                    state["opaque"] |= active
                else:
                    values[key] = values.get(key, 0) | active
            return True
        return observe

    def save(self, path):
        pool, sites = {0: 0}, {}
        for name, state in sorted(self.sites.items()):
            if not state["startup"]:
                opaque = state["opaque"]
                values = {k: pool.setdefault(v & ~opaque, len(pool)) for k,v in sorted(state["values"].items()) if v & ~opaque}
                sites[name] = [pool.setdefault(opaque, len(pool)), values]
        incomplete = set(self.tests) - (self.passed - self.failed)
        incomplete.update(t for t, bit in self.bits.items() if bit & self.external)
        data = {"tests": self.tests, "sites": sites, "pool": [format(m, "x") for m in pool],
                "incomplete": sorted(incomplete)}
        with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(data, separators=(",", ":")).encode())
        print(f"{len(sites)} truth-test sites; {len(incomplete)} incomplete tests")


class Transform(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.recorder = recorder
        self.path = path
        self.probes = []

    def condition(self, node):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            node.operand = self.condition(node.operand)
            return node
        if not isinstance(node, ast.BoolOp):
            return self.visit(node)
        leaves = []
        tree(node, leaves)
        readers = [compile_operand(n) for n in leaves]
        node.values = [self.condition(child) for child in node.values]
        if len(leaves) > 7 or not any(readers):
            return node
        name = location(self.path, node)
        func = ast.Subscript(ast.Name("_br_bool_probes", ast.Load()), ast.Constant(len(self.probes)), ast.Load())
        variables = {n.id for leaf, read in zip(leaves, readers) if read is not None
                     for n in ast.walk(leaf) if isinstance(n, ast.Name)}
        self.probes.append(self.recorder.probe(name, readers, variables))
        # The probe returns the built-in value True. Keep the original BoolOp
        # in a branching context, preserving its actual short-circuit behavior.
        return ast.copy_location(ast.BoolOp(ast.And(), [ast.Call(func, [], []), node]), node)

    def visit_If(self, node):
        for field, value in ast.iter_fields(node):
            if field == "test":
                node.test = self.condition(value)
            elif isinstance(value, list):
                setattr(node, field, [self.visit(child) for child in value])
            elif isinstance(value, ast.AST):
                setattr(node, field, self.visit(value))
        return node

    visit_While = visit_If
    visit_Assert = visit_If
    visit_IfExp = visit_If

    def visit_comprehension(self, node):
        node.target = self.visit(node.target)
        node.iter = self.visit(node.iter)
        node.ifs = [self.condition(n) for n in node.ifs]
        return node


class Loader(importlib.abc.Loader):
    def __init__(self, origin, recorder):
        self.origin, self.recorder = origin, recorder

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        transformer = Transform(self.recorder, self.recorder.relative(self.origin))
        tree = transformer.visit(ast.parse(Path(self.origin).read_bytes(), self.origin))
        module._br_bool_probes = transformer.probes
        exec(compile(ast.fix_missing_locations(tree), self.origin, "exec"), module.__dict__)
        SLOTS.register(module)


def main():
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    revision = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if revision != PINNED or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected a clean pinned checkout")
    for path in (root / "sqlglot").rglob("*.py"):
        name = path.relative_to(root).with_suffix("").as_posix().replace("/", ".").removesuffix(".__init__")
        SLOTS.add_module(name, ast.parse(path.read_bytes(), str(path)))
    tests = sorted(t["id"] for t in json.loads(inventory.read_bytes())["tests"])
    recorder = BoolRecorder(root, tests)
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
    temporary = output.with_suffix(".tmp")
    recorder.save(temporary)
    temporary.replace(output)


if __name__ == "__main__":
    main()
