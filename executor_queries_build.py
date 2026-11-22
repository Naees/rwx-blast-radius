"""Profile the queue, worker, serialization, and reference assertion for each TPC query."""
import concurrent.futures
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from executor_queries import MAPS, TARGETS
from query_checks import LocalPool

PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def cold_capture(base, node, row, out):
    """Trace one original query in an independent, clean interpreter."""
    from executor_queries import handle

    sys.path.insert(0, str(base))
    os.chdir(base)
    from tests import test_executor  # same initial imports as the complete method
    del test_executor
    prefix = str(base) + "/"
    source = prefix + "sqlglot/"
    captured = defaultdict(set)
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID

    def line(code, number):
        if code.co_filename.startswith(source):
            captured[code.co_filename[len(prefix):]].add(number)
        return monitor.DISABLE

    monitor.use_tool_id(tool, "cold-executor-query")
    monitor.register_callback(tool, monitor.events.LINE, line)
    monitor.set_events(tool, monitor.events.LINE)
    try:
        passed = handle({"tests": {node: [row]}})
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)
    if passed != [node]:
        raise RuntimeError((node, row, passed))
    out.write_text(json.dumps({p: sorted(ns) for p, ns in captured.items()}))


def guard_cold_rows(base, raw, out):
    """Keep warm/cold disagreements outside row replay.

    In particular a lazily built dispatch table is initialized by one warm
    query and consumed by others. Its builder appears in many isolated rows.
    """
    bits = [int(mask, 16) for mask in raw["pool"]]
    masks = {p: {int(n): bits[i] for n, i in lines.items()} for p, lines in raw["lines"].items()}
    guard = defaultdict(dict)
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory() as folder:
        for i, (node, (offset, times)) in enumerate(raw["rows"].items()):
            outer = 1 << i
            def run_row(row):
                output = Path(folder) / f"{row}.json"
                subprocess.run([sys.executable, script, "--cold", base, node, str(row), output],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return row, json.loads(output.read_bytes())

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
                for row, paths in workers.map(run_row, range(len(times))):
                    row_bit = 1 << (offset + row)
                    for path in masks.keys() | paths.keys():
                        warm = masks.get(path, {})
                        cold = set(paths.get(path, ()))
                        for number in warm.keys() | cold:
                            seen = warm.get(number, 0)
                            if seen & outer:
                                continue
                            if bool(seen & row_bit) != (number in cold):
                                scope = guard[path]
                                scope[number] = scope.get(number, 0) | outer
            print("Cold queries checked", node, len(times), flush=True)
    for path, lines in guard.items():
        target = masks.setdefault(path, {})
        for number, extra in lines.items():
            target[number] = target.get(number, 0) | extra
    pack(out, masks, raw["rows"], guarded=True)


def pack(out, touched, rows, guarded):
    pool = {}
    lines = {name: {line: pool.setdefault(mask, len(pool)) for line, mask in sorted(entries.items())}
             for name, entries in sorted(touched.items())}
    with out.open("wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as stream:
        stream.write(json.dumps({"rows": rows, "lines": lines, "pool": [hex(b) for b in pool],
                                 "guarded": guarded}, separators=(",", ":")).encode())


def main():
    root = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else MAPS / "executor_queries.json.gz"
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Unexpected SQLGlot revision")
    subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"], check=True)
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise ValueError("Set PYTHONHASHSEED=0")
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(root))
    os.chdir(root)
    from tests import test_executor as executor
    executor.ProcessPoolExecutor = LocalPool
    monitor = sys.monitoring
    active = 0
    prefix = str(root) + "/"
    module_prefix = prefix + "sqlglot/"
    touched, rows = defaultdict(dict), {}
    next_bit = len(TARGETS)
    elapsed = defaultdict(int)

    def activate(bit):
        nonlocal active
        active = bit
        monitor.restart_events()

    @contextmanager
    def query(bit):
        previous = active
        activate(bit)
        start = time.process_time_ns()
        try:
            yield
        finally:
            elapsed[bit] += time.process_time_ns() - start
            activate(previous)

    def line(code, lineno):
        if active and code.co_filename.startswith(module_prefix):
            entries = touched[code.co_filename[len(prefix):]]
            entries[lineno] = active | entries.get(lineno, 0)
        return monitor.DISABLE

    class TracedRows(list):
        def __iter__(self):
            for i, item in enumerate(super().__iter__()):
                with query(1 << (offset + i)):
                    yield item

    class Traced(executor.TestExecutor):
        def setUp(self):
            super().setUp()
            attr = "tpcds_sqls" if self._testMethodName.endswith("tpcds") else "tpch_sqls"
            data = TracedRows(getattr(self, attr))
            setattr(self, attr, data)
            counts.append(len(data))

        def subtestHelper(self, i, table, tpch=True):
            # A future's result is consumed after the input loop has finished.
            with query(1 << (offset + i)):
                return super().subtestHelper(i, table, tpch=tpch)

    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "executor-queries")
    monitor.register_callback(tool, monitor.events.LINE, line)
    monitor.set_events(tool, monitor.events.LINE)
    try:
        for i, node in enumerate(TARGETS):
            offset = next_bit
            counts = []
            activate(1 << i)
            result = unittest.TestSuite([Traced(node.split("::")[-1])]).run(unittest.TestResult())
            activate(0)
            if not result.wasSuccessful() or result.skipped or len(counts) != 1:
                raise ValueError((node, result.errors[:1], result.failures[:1], result.skipped))
            n = counts[0]
            weights = [max(1, elapsed[1 << (offset + j)] // 1000) for j in range(n)]
            rows[node] = (offset, weights)
            next_bit += n
            print(node, n, round(sum(weights) / 1_000_000, 2), flush=True)
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)

    with tempfile.TemporaryDirectory() as directory:
        warm = Path(directory) / "warm.json.gz"
        pack(warm, touched, rows, guarded=False)
        with gzip.open(warm, "rt") as input_map:
            guard_cold_rows(root, json.load(input_map), out)


if __name__ == "__main__":
    if sys.argv[1] == "--cold":
        os.environ["SKIP_INTEGRATION"] = "0"
        cold_capture(Path(sys.argv[2]).resolve(), sys.argv[3], int(sys.argv[4]), Path(sys.argv[5]))
    elif sys.argv[1] == "--merge":
        with gzip.open(sys.argv[3], "rt") as input_map:
            guard_cold_rows(Path(sys.argv[2]).resolve(), json.load(input_map), Path(sys.argv[4]))
    else:
        main()
