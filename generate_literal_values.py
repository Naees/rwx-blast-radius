"""Record bounded primitive values used in literal comparisons in baseline functions."""

import ast
import concurrent.futures
import gzip
import importlib.abc
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from generate_map import PINNED, Recorder, TracedPool
from literal_values import key, shape
from observe_base import ProbeFinder


class Literals(Recorder):
    def __init__(self, root, tests):
        super().__init__(root, tests)
        self.sites = {}
        self.external = 0

    def activate(self, mask):
        self.active = mask

    def audit(self, event, args):
        if event == "subprocess.Popen":
            self.external |= self.active

    def probe(self, name):
        state = self.sites.setdefault(name, {"values": {}, "opaque": 0, "startup": False})
        values = state["values"]
        def track(value):
            active = self.active
            if not active:
                state["startup"] = True
            elif not state["startup"] and active & ~state["opaque"]:
                tagged = key(value)
                if tagged is None or (tagged not in values and len(values) >= 160):
                    state["opaque"] |= active
                else:
                    prior = values.get(tagged, 0)
                    if active & ~prior:
                        values[tagged] = prior | active
            return value
        return track

    def save(self, output):
        pool = {0: 0}
        sites = {}
        for name, state in sorted(self.sites.items()):
            if state["startup"]:
                continue
            unknown = state["opaque"]
            encoded = []
            for value, mask in sorted(state["values"].items(), key=lambda item: repr(item[0])):
                mask &= ~unknown
                if mask:
                    encoded.append([value, pool.setdefault(mask, len(pool))])
            sites[name] = [pool.setdefault(unknown, len(pool)), encoded]
        incomplete = set(self.tests) - (self.passed - self.failed)
        incomplete.update(t for t, bit in self.bits.items() if bit & self.external)
        data = {"tests": self.tests, "sites": sites, "pool": [format(m, "x") for m in pool],
                "incomplete": sorted(incomplete)}
        with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(data, separators=(",", ":")).encode())
        print(f"{len(sites)} sites, {len(incomplete)} incomplete tests")


class Transform(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.recorder = recorder
        self.path = path
        self.probes = []
        self.local = False

    def visit_FunctionDef(self, node):
        prior, self.local = self.local, True
        node.body = [self.visit(x) for x in node.body]
        self.local = prior
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        prior, self.local = self.local, True
        node.body = self.visit(node.body)
        self.local = prior
        return node

    def visit_ClassDef(self, node):
        prior, self.local = self.local, False
        node.body = [self.visit(x) for x in node.body]
        self.local = prior
        return node

    def visit_Compare(self, node):
        info = shape(node) if self.local else None
        self.generic_visit(node)
        if not info:
            return node
        side = info[1]
        location = f"{self.path}:{node.lineno}:{node.col_offset}:{side}"
        fn = ast.Subscript(ast.Name("_br_literal_values", ast.Load()),
                           ast.Constant(len(self.probes)), ast.Load())
        self.probes.append(self.recorder.probe(location))
        variable = node.left if side == "r" else node.comparators[0]
        traced = ast.copy_location(ast.Call(fn, [variable], []), variable)
        if side == "r":
            node.left = traced
        else:
            node.comparators[0] = traced
        return node


class Loader(importlib.abc.Loader):
    def __init__(self, origin, recorder):
        self.origin = origin
        self.recorder = recorder

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        source = Path(self.origin).read_bytes()
        instrument = Transform(self.recorder, self.recorder.relative(self.origin))
        tree = instrument.visit(ast.parse(source, self.origin))
        module._br_literal_values = instrument.probes
        exec(compile(ast.fix_missing_locations(tree), self.origin, "exec"), module.__dict__)


def main():
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    rev = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if rev != PINNED or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected the clean pinned checkout")
    tests = sorted(r["id"] for r in json.loads(inventory.read_bytes())["tests"])
    recorder = Literals(root, tests)
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
