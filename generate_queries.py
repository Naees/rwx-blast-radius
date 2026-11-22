"""Attribute execution in optimizer fixture loops to individual SQL pairs."""

import concurrent.futures
import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from generate_map import PINNED
from query_checks import LocalPool


def main():
    base, inventory, out = (Path(s).resolve() for s in sys.argv[1:])
    if subprocess.check_output(["git", "-C", base, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Unexpected SQLGlot revision")
    if subprocess.run(["git", "-C", base, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected clean base checkout")
    ids = {r["id"] for r in json.loads(inventory.read_bytes())["tests"]}
    sys.path.insert(0, str(base))
    os.chdir(base)
    from tests import test_optimizer as opt

    outside_tests = sorted(n for n in ids if n.startswith("tests/test_optimizer.py::"))
    outside_bits = {node: 1 << i for i, node in enumerate(outside_tests)}

    class Trace:
        node = None
        active = 0

        def select(self, bit):
            self.active = bit
            if bit:
                sys.monitoring.restart_events()

        def pytest_collection_modifyitems(self, items):
            items[:] = [item for item in items if item.nodeid in ids]

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item):
            self.node = item.nodeid
            self.select(outside_bits[item.nodeid])
            try:
                return (yield)
            finally:
                self.select(0)
                self.node = None

        @pytest.hookimpl(wrapper=True)
        def pytest_fixture_setup(self, fixturedef, request):
            previous = self.active
            if fixturedef.scope in {"class", "module", "session"}:
                prefix = request.node.nodeid
                self.select(sum(bit for name, bit in outside_bits.items()
                                if not prefix or name.startswith(prefix + "::")))
            try:
                return (yield)
            finally:
                self.select(previous)

    plugin = Trace()
    pool, lines, cases, futures = {}, defaultdict(dict), {}, {}
    prefix = str(base) + "/"
    def line(code, lineno):
        if plugin.active and code.co_filename.startswith(prefix + "sqlglot/"):
            rows = lines[code.co_filename[len(prefix):]]
            rows[lineno] = rows.get(lineno, 0) | plugin.active
        return sys.monitoring.DISABLE

    old_loader, old_completed = opt.load_sql_fixture_pairs, opt.as_completed
    def loader(path):
        prior = plugin.active
        # check_file associates submitted jobs with individual fixture pairs.
        # The two annotation tests validate independently within the yield.
        caller = sys._getframe(1).f_code
        from_check = caller.co_filename == opt.__file__ and caller.co_name in {
            "check_file", "test_annotate_funcs", "test_annotate_types",
        }
        for i, pair in enumerate(old_loader(path)):
            if from_check and plugin.node:
                bit = cases.setdefault((plugin.node, path, i), len(cases))
                plugin.select(1 << (len(outside_tests) + bit))
            try:
                yield pair
            finally:
                plugin.select(prior)

    def completed(results):
        for future in old_completed(results):
            prior = plugin.active
            plugin.select(futures.pop(future, prior))
            try:
                yield future
            finally:
                plugin.select(prior)

    class MarkPool(LocalPool):
        def submit(self, fn, *args, **kwargs):
            f = super().submit(fn, *args, **kwargs)
            futures[f] = plugin.active
            return f
    opt.load_sql_fixture_pairs, opt.as_completed, opt.ProcessPoolExecutor = loader, completed, MarkPool
    m = sys.monitoring
    m.use_tool_id(m.COVERAGE_ID, "optimizer-fixtures")
    m.register_callback(m.COVERAGE_ID, m.events.LINE, line)
    m.set_events(m.COVERAGE_ID, m.events.LINE)
    rc = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests/test_optimizer.py"], plugins=[plugin])
    m.set_events(m.COVERAGE_ID, 0)
    m.free_tool_id(m.COVERAGE_ID)
    if rc != 0:
        raise RuntimeError(rc)
    encoded = {k: {n: pool.setdefault(bits,len(pool)) for n,bits in sorted(v.items())} for k,v in sorted(lines.items())}
    with out.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps({"outside_tests": outside_tests, "cases": list(cases), "lines": encoded, "pool": [hex(b) for b in pool]},
                           separators=(",", ":")).encode())
    print(f"Captured {len(cases)} fixture cases")


if __name__ == "__main__":
    main()
