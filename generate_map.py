"""Collect dependencies from an unmodified pinned checkout on CPython 3.13.

Usage: python generate_map.py SQLGLOT_DIR TESTS_JSON maps/deps.json.gz
Install the test dependencies from the starter kit requirements.lock, including
pytest, duckdb, pandas, numpy and setuptools. Collection runs the listed tests
with SKIP_INTEGRATION=0 and traces worker serialization and child interpreters.
For an existing map, --resume-incomplete retries unfinished tests, and
--refresh-module tests/test_example.py extends measurement for a test module.
"""

import argparse
import concurrent.futures
import gzip
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from functools import lru_cache
from multiprocessing.reduction import ForkingPickler
from pathlib import Path

import pytest

from profile_child.source_reads import direct_reader


PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
MONITOR = sys.monitoring


class TracedPool(concurrent.futures.ThreadPoolExecutor):
    """Keep pool code visible to the tracer, including process-boundary pickling."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer, initargs = self.roundtrip((initializer, initargs))
        super().__init__(max_workers=1, initializer=initializer, initargs=initargs)

    @staticmethod
    def roundtrip(value):
        return ForkingPickler.loads(ForkingPickler.dumps(value))

    def submit(self, fn, /, *args, **kwargs):
        fn, args, kwargs = self.roundtrip((fn, args, kwargs))

        def job():
            return self.roundtrip(fn(*args, **kwargs))

        return super().submit(job)


class Recorder:
    def __init__(self, root, tests):
        self.prefix = str(root) + os.sep
        self.tests = tests
        self.bits = {node: 1 << i for i, node in enumerate(tests)}
        self.class_bits = defaultdict(int)
        self.module_bits = defaultdict(int)
        for node, bit in self.bits.items():
            self.class_bits[node.rsplit("::", 1)[0]] |= bit
            self.module_bits[node.split("::", 1)[0]] |= bit
        self.lines = defaultdict(dict)
        self.reads = defaultdict(int)
        self.source_reads = defaultdict(int)
        self.passed = set()
        self.failed = set()
        self.active = 0
        self.only = set(tests)

    @lru_cache(None)
    def relative(self, filename):
        if filename.startswith(self.prefix):
            relative = filename[len(self.prefix):]
            if not relative.startswith(".git/") and "__pycache__" not in relative:
                return relative
        return None

    def record_line(self, code, number):
        if self.active:
            path = self.relative(code.co_filename)
            if path:
                lines = self.lines[path]
                lines[number] = lines.get(number, 0) | self.active
        return MONITOR.DISABLE

    def audit(self, event, args):
        if event == "open" and self.active:
            filename = args[0]
            if isinstance(filename, str) and not filename.endswith(".pyc"):
                path = self.relative(os.path.abspath(filename))
                if path:
                    self.reads[path] |= self.active
                    if path.endswith(".py") and direct_reader(sys._getframe(1), self.relative):
                        self.source_reads[path] |= self.active

    def activate(self, mask):
        if self.active != mask:
            self.active = mask
            os.environ["BLAST_TRACE_MASK"] = format(mask, "x")
            MONITOR.restart_events()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.activate(self.bits.get(item.nodeid, 0))
        try:
            return (yield)
        finally:
            self.activate(0)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        original = self.active
        if fixturedef.scope == "class":
            self.activate(self.class_bits.get(request.node.nodeid, original))
        elif fixturedef.scope == "module":
            self.activate(self.module_bits.get(request.node.nodeid, original))
        try:
            return (yield)
        finally:
            self.activate(original)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if item.nodeid in self.only]

    def resume(self, path):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        if data["tests"] != self.tests:
            raise ValueError("Cannot resume with a different inventory")
        pool = [int(mask, 16) for mask in data["pool"]]
        for name, lines in data["files"].items():
            self.lines[name] = {int(n): pool[i] for n, i in lines.items()}
        self.reads.update({name: pool[i] for name, i in data["reads"].items()})
        self.source_reads.update({name: pool[i] for name, i in data.get("source_reads", {}).items()})
        self.only = set(data["incomplete"])
        self.passed = set(self.tests) - self.only

    def merge_children(self, directory):
        for child in Path(directory).glob("*.json"):
            data = json.loads(child.read_bytes())
            mask = int(data["mask"], 16)
            for name, numbers in data["files"].items():
                lines = self.lines[name]
                for n in numbers:
                    lines[n] = lines.get(n, 0) | mask
            for name in data["reads"]:
                self.reads[name] |= mask
            for name in data.get("source_reads", []):
                self.source_reads[name] |= mask

    def save(self, path):
        # Many adjacent statements share an exact test bitset. Intern them once.
        pool = {}
        def intern(mask):
            if mask not in pool:
                pool[mask] = len(pool)
            return pool[mask]

        files = {}
        for name, lines in sorted(self.lines.items()):
            files[name] = {line: intern(mask) for line, mask in sorted(lines.items())}
        reads = {name: intern(mask) for name, mask in sorted(self.reads.items())}
        sources = {name: intern(mask) for name, mask in sorted(self.source_reads.items())}
        missing = sorted(set(self.tests) - (self.passed - self.failed))
        data = {"tests": self.tests, "incomplete": missing, "files": files,
                "reads": reads, "source_reads": sources, "pool": [format(mask, "x") for mask in pool]}
        with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(json.dumps(data, separators=(",", ":")).encode())
        print(f"Mapped {len(self.tests)} tests in {len(files)} files; {len(missing)} incomplete")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--resume-incomplete", action="store_true")
    parser.add_argument("--refresh-source-reads", action="store_true")
    parser.add_argument("--refresh-module", action="append", default=[])
    args = parser.parse_args()
    root, inventory, output = (p.resolve() for p in (args.root, args.inventory, args.output))
    revision = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if revision != PINNED:
        raise ValueError(f"Expected pinned revision {PINNED}, found {revision}")
    if subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Tracked files differ from the pinned revision")
    tests = sorted(row["id"] for row in json.loads(inventory.read_text())["tests"])
    recorder = Recorder(root, tests)
    if args.resume_incomplete or args.refresh_module or args.refresh_source_reads:
        recorder.resume(output)
        recorder.only.update(t for t in tests if t.split("::")[0] in args.refresh_module)
        if args.refresh_source_reads:
            mask = 0
            for path, observed in recorder.reads.items():
                if path.endswith(".py"):
                    mask |= observed
            recorder.only.update(t for t in tests if recorder.bits[t] & mask)
        if not recorder.only:
            return
    child_path = Path(__file__).resolve().parent / "profile_child"
    child_dir = tempfile.TemporaryDirectory()
    os.environ["BLAST_TRACE_DIR"] = child_dir.name
    os.environ["BLAST_TRACE_ROOT"] = str(root)
    os.environ["PYTHONPATH"] = os.pathsep.join([str(child_path), str(root), os.environ.get("PYTHONPATH", "")])
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, str(root))
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.addaudithook(recorder.audit)
    tool = MONITOR.COVERAGE_ID
    MONITOR.use_tool_id(tool, "per-test-dependencies")
    MONITOR.register_callback(tool, MONITOR.events.LINE, recorder.record_line)
    MONITOR.set_events(tool, MONITOR.events.LINE)
    try:
        result = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"],
                             plugins=[recorder])
    finally:
        MONITOR.set_events(tool, 0)
        MONITOR.free_tool_id(tool)
    if result not in {0, 1}:
        raise SystemExit(result)
    recorder.merge_children(child_dir.name)
    temporary = output.with_suffix(".tmp")
    recorder.save(temporary)
    temporary.replace(output)
    child_dir.cleanup()


if __name__ == "__main__":
    main()
