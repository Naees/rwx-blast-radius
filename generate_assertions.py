"""Collect which SQLGlot lines each standalone dialect assertion exercises."""

import ast
import functools
import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from generate_map import PINNED


HELPERS = {"validate_identity", "validate_transpile", "validate_all"}


def independent(line):
    """The original test discards the validator result and supplies constant inputs."""
    if not isinstance(line, ast.Expr) or not isinstance(line.value, ast.Call):
        return False
    call = line.value
    f = call.func
    if not (isinstance(f, ast.Attribute) and f.attr in HELPERS
            and isinstance(f.value, ast.Name) and f.value.id == "self"):
        return False
    for arg in call.args + [kw.value for kw in call.keywords]:
        try:
            ast.literal_eval(arg)
        except (ValueError, TypeError):
            return False
    return True


def index_steps(base, inventory):
    result = []
    for path in sorted((base / "tests/dialects").glob("test_*.py")):
        filename = str(path.relative_to(base))
        for cls in ast.parse(path.read_bytes()).body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef) or fn.decorator_list:
                    continue
                node = f"{filename}::{cls.name}::{fn.name}"
                if node not in inventory:
                    continue
                for stmt in fn.body:
                    if independent(stmt):
                        result.append([node, stmt.lineno, stmt.end_lineno])
    return result


class AssertionTrace:
    def __init__(self, base, steps):
        self.base = str(base) + "/"
        self.by_location = {}
        self.eligible = {node for node, _, _ in steps}
        self.outside_tests = sorted(self.eligible)
        self.outside_bits = {node: 1 << (i + len(steps))
                             for i, node in enumerate(self.outside_tests)}
        for i, (node, lo, hi) in enumerate(steps):
            for line in range(lo, hi + 1):
                self.by_location[node, line] = 1 << i
        self.lines = defaultdict(dict)
        self.active = 0
        self.test = None
        self.completed = set()
        self.failed = set()
        self.hit_steps = 0

    def trace_line(self, code, line):
        path = code.co_filename
        if self.active and path.startswith(self.base + "sqlglot/"):
            table = self.lines[path[len(self.base):]]
            table[line] = table.get(line, 0) | self.active
        return sys.monitoring.DISABLE

    def wrap(self, fn):
        @functools.wraps(fn)
        def tracked(case, *args, **kwargs):
            frame = sys._getframe(1)
            bit = self.by_location.get((self.test, frame.f_lineno), 0)
            if not bit or not frame.f_code.co_filename.startswith(self.base + "tests/dialects/"):
                return fn(case, *args, **kwargs)
            previous = self.active
            self.active = bit
            sys.monitoring.restart_events()
            try:
                value = fn(case, *args, **kwargs)
                self.hit_steps |= bit
                return value
            finally:
                self.active = previous
                if previous:
                    sys.monitoring.restart_events()
        return tracked

    def pytest_collection_modifyitems(self, items):
        items[:] = [i for i in items if i.nodeid in self.eligible]

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        self.test = item.nodeid
        self.active = self.outside_bits[item.nodeid]
        sys.monitoring.restart_events()
        try:
            return (yield)
        finally:
            self.test = None
            self.active = 0

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        previous = self.active
        if fixturedef.scope in {"class", "module", "session"}:
            prefix = request.node.nodeid
            self.active = sum(bit for name, bit in self.outside_bits.items()
                              if not prefix or name.startswith(prefix + "::"))
            sys.monitoring.restart_events()
        try:
            return (yield)
        finally:
            self.active = previous
            sys.monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.completed.add(report.nodeid)


def main():
    base, inventory, output = (Path(a).resolve() for a in sys.argv[1:])
    if subprocess.check_output(["git", "-C", base, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Unexpected revision")
    if subprocess.run(["git", "-C", base, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected a clean base checkout")
    ids = {r["id"] for r in json.loads(inventory.read_bytes())["tests"]}
    steps = index_steps(base, ids)
    recorder = AssertionTrace(base, steps)
    os.chdir(base)
    sys.path.insert(0, str(base))
    from tests.dialects.test_dialect import Validator
    for name in HELPERS:
        setattr(Validator, name, recorder.wrap(getattr(Validator, name)))
    m = sys.monitoring
    m.use_tool_id(m.COVERAGE_ID, "dialect-assertions")
    m.register_callback(m.COVERAGE_ID, m.events.LINE, recorder.trace_line)
    m.set_events(m.COVERAGE_ID, m.events.LINE)
    try:
        result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests/dialects"],
                             plugins=[recorder])
    finally:
        m.set_events(m.COVERAGE_ID, 0)
        m.free_tool_id(m.COVERAGE_ID)
    if result not in (0, 1):
        raise RuntimeError(result)
    good = recorder.completed - recorder.failed
    mask = sum(1 << i for i, (name, lo, hi) in enumerate(steps) if name in good)
    known = recorder.hit_steps & mask
    pool, lines = {}, {}
    for path, data in sorted(recorder.lines.items()):
        lines[path] = {line: pool.setdefault(bits, len(pool)) for line, bits in sorted(data.items())}
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        encoded = {"outside_tests": recorder.outside_tests, "steps": steps, "known": hex(known), "pool": [hex(k) for k in pool], "lines": lines}
        f.write(json.dumps(encoded, separators=(",", ":")).encode())
    print(f"Captured {known.bit_count()}/{len(steps)} assertions over {len(good)} test methods")


if __name__ == "__main__":
    main()
