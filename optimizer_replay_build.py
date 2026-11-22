"""Build query-level coverage and semantic snapshots from the unmodified revision.

Run PYTHONHASHSEED=0 python optimizer_replay_build.py /path/to/pinned/sqlglot
with the dependencies from the starter kit's requirements.lock.
"""
import ast
import concurrent.futures
import hashlib
import inspect
import io
import gzip
import json
import os
import pickle
import re
import zlib
import subprocess
import sys
import unittest
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from optimizer_replay import MAPS, PREFIXES, TARGETS, fingerprint

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class InlinePool:
    """Execute the pool's per-job serialization round trip in the tracing interpreter."""
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def submit(self, function, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            args = tuple(arg if callable(arg) else pickle.loads(pickle.dumps(arg)) for arg in args)
            kwargs = pickle.loads(pickle.dumps(kwargs))
            value = function(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(value)))
        except Exception as e:
            future.set_exception(e)
        return future

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def shutdown(self, **kwargs):
        pass


class Capture:
    def __init__(self, root):
        self.prefix = str(root) + os.sep
        self.touch = defaultdict(dict)
        self.calls = defaultdict(int)
        self.inputs = []
        self.data = io.BytesIO()
        self.locations = {}
        self.rules = ()
        self.current = 0
        self.active = 0

    @lru_cache(maxsize=None)
    def relative(self, path):
        if path.startswith(self.prefix):
            path = path[len(self.prefix):]
            if path.startswith(PREFIXES):
                return path

    def line(self, code, lineno):
        if self.active:
            path = self.relative(code.co_filename)
            if path:
                entries = self.touch[path]
                entries[lineno] = entries.get(lineno, 0) | self.active
        return sys.monitoring.DISABLE

    def activate(self, mask):
        self.active = mask
        sys.monitoring.restart_events()

    def wrap(self, method, function):
        def captured(data, *args, **kwargs):
            from sqlglot import Dialect
            from sqlglot.schema import MappingSchema

            parent = self.active
            if not parent:
                return function(data, *args, **kwargs)
            options = dict(kwargs)
            value = data
            if method.startswith("rule:"):
                value = data.dump()
            elif method == "optimize":
                if len(args) > 1:
                    return function(data, *args, **kwargs)
                if args:
                    options["schema"] = args[0]
                schema = options.get("schema")
                if isinstance(schema, MappingSchema):
                    if (schema.visible or schema.udf_mapping or not schema.normalize or
                            type(schema.dialect) is not Dialect):
                        return function(data, *args, **kwargs)
                    options["schema"] = schema.mapping
                value = data.dump()
                kwargs = {**kwargs, "rules": self.rules}
            elif args or not isinstance(data, str):
                return function(data, *args, **kwargs)
            try:
                json.dumps(options)
            except (ValueError, TypeError):
                return function(data, *args, **kwargs)
            index = len(self.inputs)
            self.inputs.append(None)
            bit = 1 << (len(TARGETS) + index)
            self.calls[self.current] |= bit
            self.activate(bit)
            try:
                result = function(data, *args, **kwargs)
                self.activate(0)
                raw = json.dumps(value, separators=(",", ":")).encode()
                digest = hashlib.sha256(raw).digest()
                if digest not in self.locations:
                    offset = self.data.tell()
                    length = self.data.write(zlib.compress(raw, level=4))
                    self.locations[digest] = offset, length
                self.inputs[index] = [method, *self.locations[digest], options, fingerprint(result)]
            finally:
                self.activate(parent)
            return result
        captured.__signature__ = inspect.signature(function)
        return captured


def save(path, data):
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps(data, separators=(",", ":")).encode())


def private_constants(root):
    # Keep scalar initializer probes confined to optimizer constants whose named
    # consumers all occur in the same module at the pinned revision.
    owners = defaultdict(set)
    assignments = {}
    for parent in ("sqlglot", "tests"):
        for path in (root / parent).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            source = path.read_text()
            for name in set(re.findall(r"\b[A-Z][A-Z_0-9]+\b", source)):
                owners[name].add(relative)
            if relative.startswith("sqlglot/optimizer/"):
                names = set()
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id.isupper():
                                names.add(target.id)
                assignments[relative] = names
    return {path: sorted(n for n in names if owners[n] == {path})
            for path, names in assignments.items()}


def main():
    root = Path(sys.argv[1]).resolve()
    revision = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != PIN:
        raise ValueError("Unexpected SQLGlot revision")
    subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"], check=True)
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise ValueError("Generate with PYTHONHASHSEED=0 to match the submission image")
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(root))
    os.chdir(root)
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from sqlglot import executor, optimizer
    from tests import test_executor, test_optimizer

    recorder = Capture(root)
    rules = []
    for rule in optimizer.RULES:
        # Rules that accept a mutable schema share state with later rules.
        # The outer optimize() boundary handles these calls.
        if "schema" not in inspect.signature(rule).parameters:
            label = "rule:" + rule.__module__ + "." + rule.__name__
            rule = recorder.wrap(label, rule)
        rules.append(rule)
    recorder.rules = tuple(rules)
    executor.optimize = optimizer.optimize = recorder.wrap("optimize", optimizer.optimize)
    test_executor.parse_one = recorder.wrap("parse_one", test_executor.parse_one)
    test_executor.transpile = recorder.wrap("transpile", test_executor.transpile)

    valid = []
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "test-replay")
    monitor.register_callback(tool, monitor.events.LINE, recorder.line)
    monitor.set_events(tool, monitor.events.LINE)
    try:
        for i, name in enumerate(TARGETS):
            filename, class_name, method = name.split("::")
            module = test_optimizer if filename.endswith("test_optimizer.py") else test_executor
            recorder.current = name
            recorder.activate(1 << i)
            suite = unittest.TestSuite([getattr(module, class_name)(method)])
            result = suite.run(unittest.TestResult())
            recorder.activate(0)
            if result.wasSuccessful() and not result.skipped:
                valid.append(name)
            else:
                print(name, result.errors[:1], result.failures[:1], file=sys.stderr)
            print(name, len(recorder.inputs), flush=True)
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)

    if len(valid) != len(TARGETS) or None in recorder.inputs:
        raise SystemExit("Incomplete baseline test capture")
    pool = {}
    coverage = {
        path: {line: pool.setdefault(mask, len(pool)) for line, mask in sorted(lines.items())}
        for path, lines in sorted(recorder.touch.items())
    }
    index = {
        "records": recorder.inputs,
        "private": private_constants(root),
        "valid": valid,
        "calls": {test: format(mask, "x") for test, mask in recorder.calls.items()},
        "coverage": coverage,
        "pool": [format(mask, "x") for mask in pool],
    }
    (MAPS / "optimizer_replay.bin").write_bytes(recorder.data.getvalue())
    save(MAPS / "optimizer_replay.json.gz", index)


if __name__ == "__main__":
    main()
