"""Capture comparison operand relationships and boolean outcomes on the pinned base.

Run with the same arguments and environment as generate_map.py. Expressions are
instrumented on import. No patched version of any source is loaded.

A fresh invocation measures every inventory entry. To refresh specific test
modules in an existing map, append --refresh-module tests/test_example.py
(repeatable). The refresh removes older observations for each selected test;
other tests retain their prior measurements. Use --identity-only with an existing
map to rerun every inventory test while replacing just the identity comparisons.
The recorder checks operands against the pinned class methods without invoking
unknown user-defined comparisons.
"""

import ast
import argparse
import concurrent.futures
import gzip
import importlib.abc
import importlib.machinery
import json
import operator
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from generate_map import PINNED, Recorder, TracedPool
from identity_facts import IdentityFacts


OPS = {"Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
       "Gt": operator.gt, "GtE": operator.ge, "Is": operator.is_, "IsNot": operator.is_not}
SCALARS = {int, bool, str, bytes, float}


def primitive(value):
    kind = type(value)
    return kind in SCALARS or (kind is tuple and all(type(x) in SCALARS for x in value))


def relation(left, right, original, result):
    # Each observation records identity plus one relationship: equality,
    # inequality, a total-order relationship, or unknown.
    same = left is right
    if original in ("Eq", "NotEq"):
        rel = ("=" if result == (original == "Eq") else "!") if type(result) is bool else "?"
    elif primitive(left) and primitive(right) and type(result) is bool:
        if left == right:
            rel = "="
        elif left < right:
            rel = "<"
        elif left > right:
            rel = ">"
        else:
            rel = "?"  # NaN
    else:
        rel = "?"
    return ("1" if same else "0") + rel


class ObservationRecorder(Recorder):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.sites = {}
        self.child_process_tests = 0
        self.identity = IdentityFacts()
        self.identity_only = False

    def activate(self, mask):
        self.active = mask

    def audit_child(self, event, args):
        if event == "subprocess.Popen":
            self.child_process_tests |= self.active

    def make_probe(self, path, node, kind):
        key = f"{path}:{node.lineno}:{node.col_offset}:{kind}"
        observations = self.sites.setdefault(key, {})
        last = {}
        def capture(state):
            active = self.active
            if active and last.get(state) != active:
                last[state] = active
                observations[state] = observations.get(state, 0) | active

        if kind in ("And", "Or"):
            def boolean(value):
                value = bool(value)
                capture("T" if value else "F")
                return value
            return boolean

        if kind in ("Is", "IsNot"):
            negate = kind == "IsNot"
            def identity(left, right):
                if self.active:
                    capture(self.identity.state(left, right))
                return (left is right) != negate
            return identity

        op = OPS[kind]
        def compare(left, right):
            try:
                result = op(left, right)
            except BaseException:
                capture("error")
                raise
            if self.active:
                capture(relation(left, right, kind, result))
            return result
        return compare

    def resume(self, output):
        with gzip.open(output, "rt") as stream:
            data = json.load(stream)
        if data["tests"] != self.tests:
            raise ValueError("Cannot resume with a different inventory")
        pool = [int(mask, 16) for mask in data["pool"]]
        for site, states in data["sites"].items():
            self.sites[site] = {key: pool[index] for key, index in states.items()}
        self.only = set(data["incomplete"])
        self.passed = set(self.tests) - self.only

    def forget(self, tests):
        # Drop stale observations for remeasured tests before adding fresh ones.
        mask = sum(self.bits[t] for t in tests)
        for site, states in self.sites.items():
            self.sites[site] = {state: bits & ~mask for state, bits in states.items() if bits & ~mask}
        self.passed.difference_update(tests)

    def save(self, output):
        pool = {}
        sites = {}
        for site, states in sorted(self.sites.items()):
            sites[site] = {s: pool.setdefault(mask, len(pool)) for s, mask in sorted(states.items())}
        partial = set(self.tests) - (self.passed - self.failed)
        partial.update(t for t in self.tests if self.bits[t] & self.child_process_tests)
        data = {"tests": self.tests, "incomplete": sorted(partial),
                "pool": [format(mask, "x") for mask in pool], "sites": sites}
        with open(output, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(data, separators=(",", ":")).encode())
        print(f"Recorded {len(sites)} comparison/condition sites; {len(data['incomplete'])} incomplete")


class Instrument(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.recorder = recorder
        self.path = path
        self.probes = []

    def wrap(self, node, kind, args):
        index = len(self.probes)
        self.probes.append(self.recorder.make_probe(self.path, node, kind))
        fn = ast.Subscript(ast.Name("_br_condition_probes", ast.Load()), ast.Constant(index), ast.Load())
        return ast.copy_location(ast.Call(fn, args, []), node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        allowed = ("Is", "IsNot") if self.recorder.identity_only else OPS
        if len(node.ops) == 1 and (kind := type(node.ops[0]).__name__) in allowed:
            return self.wrap(node, kind, [node.left, node.comparators[0]])
        return node

    def condition(self, node):
        if isinstance(node, ast.BoolOp) and not self.recorder.identity_only:
            node.values = [self.condition(value) for value in node.values]
            return self.wrap(node, type(node.op).__name__, [node])
        return self.visit(node)

    def visit_If(self, node):
        # Only force bool() in a truth-test context, preserving the value
        # returned by an ordinary short-circuit return/assignment.
        for field, value in ast.iter_fields(node):
            if field == "test":
                node.test = self.condition(value)
            elif isinstance(value, list):
                setattr(node, field, [self.visit(part) for part in value])
            elif isinstance(value, ast.AST):
                setattr(node, field, self.visit(value))
        return node

    visit_IfExp = visit_If
    visit_While = visit_If
    visit_Assert = visit_If


class ProbeLoader(importlib.abc.Loader):
    def __init__(self, origin, recorder):
        self.origin = origin
        self.recorder = recorder

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        source = Path(self.origin).read_bytes()
        instrument = Instrument(self.recorder, self.recorder.relative(self.origin))
        tree = ast.parse(source, self.origin)
        gates = self.recorder.identity.type_gates(tree)
        tree = instrument.visit(tree)
        module._br_condition_probes = instrument.probes
        exec(compile(ast.fix_missing_locations(tree), self.origin, "exec"), module.__dict__)
        if gates:
            self.recorder.identity.add_type_gates(module, gates)


class ProbeFinder(importlib.abc.MetaPathFinder):
    def __init__(self, recorder, loader=ProbeLoader):
        self.recorder = recorder
        self.loader = loader

    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py") and self.recorder.relative(spec.origin):
                spec.loader = self.loader(spec.origin, self.recorder)
                return spec
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--refresh-module", action="append", default=[])
    modes.add_argument("--identity-only", action="store_true",
                       help="replace every identity site in a complete existing map")
    args = parser.parse_args()
    root, inventory, output = (p.resolve() for p in (args.root, args.inventory, args.output))
    revision = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if revision != PINNED or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected a clean pinned SQLGlot checkout")
    tests = sorted(x["id"] for x in json.loads(inventory.read_text())["tests"])
    recorder = ObservationRecorder(root, tests)
    previous_partial = set()
    if args.identity_only:
        recorder.resume(output)
        previous_partial = set(recorder.only)
        recorder.only = set(tests)
        recorder.passed.clear()
        recorder.identity_only = True
        recorder.sites = {k: v for k, v in recorder.sites.items() if not k.endswith((":Is", ":IsNot"))}
    if args.refresh_module:
        recorder.resume(output)
        recorder.only.update(t for t in tests if t.split("::")[0] in args.refresh_module)
        recorder.forget(recorder.only)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(root))
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    sys.addaudithook(recorder.audit_child)
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.meta_path.insert(0, ProbeFinder(recorder))
    result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[recorder])
    if result not in (0, 1):
        raise SystemExit(result)
    temporary = output.with_suffix(".tmp")
    recorder.failed.update(previous_partial)
    recorder.save(temporary)
    temporary.replace(output)


if __name__ == "__main__":
    main()
