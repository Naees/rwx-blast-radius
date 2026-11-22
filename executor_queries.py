"""Run the original executor assertions for TPC queries that reached a source edit."""
from functools import lru_cache
from pathlib import Path

TARGETS = tuple("tests/test_executor.py::TestExecutor::" + name for name in (
    "test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch",
))
MAPS = Path(__file__).resolve().parent / "maps"


@lru_cache(maxsize=1)
def index():
    from mapdata import load
    return load(MAPS / "executor_queries.json.gz")


@lru_cache(maxsize=1)
def initializers():
    from mapdata import load
    return load(MAPS / "initializers.json.gz")


def needed(patch, impact, candidates):
    if (not set(candidates).intersection(TARGETS) or patch.old_path != patch.new_path
            or not patch.path.startswith("sqlglot/") or not patch.path.endswith(".py")
            or impact.global_change or impact.equivalent or impact.invalid or not impact.lines):
        return {}
    # Code invoked at import can change a binding shared across every row.
    if any(not impact.lines.isdisjoint(lines)
           for lines in initializers().get(patch.path, {}).values()):
        return {}
    data = index()
    if not data.get("guarded"):
        return {}
    table = data["lines"].get(patch.path, {})
    bits = 0
    for line in impact.lines:
        pos = table.get(str(line))
        if pos is not None:
            bits |= int(data["pool"][pos], 16)
    tests = {}
    for i, node in enumerate(TARGETS):
        if node not in candidates or bits & (1 << i):
            continue
        offset, weights = data["rows"][node]
        active = [j for j in range(len(weights)) if bits & (1 << (offset + j))]
        if not active:
            continue
        seconds = sum(weights[j] for j in active) / 1_000_000
        if seconds > min(2.5, max(.25, .45 * sum(weights) / 1_000_000)):
            continue
        tests[node] = active
    return tests


def handle(payload):
    import unittest
    from query_checks import LocalPool
    from tests import test_executor as executor

    # The upstream helper indexes the same list to find the reference DuckDB SQL.
    # Keep the original query, metadata and reference aligned through that lookup.
    class Selected(executor.TestExecutor):
        def setUp(self):
            super().setUp()
            attr = "tpcds_sqls" if self._testMethodName.endswith("tpcds") else "tpch_sqls"
            rows = getattr(self, attr)
            setattr(self, attr, [rows[i] for i in by_method[self._testMethodName]])
            self.cache = {}

    executor.ProcessPoolExecutor = LocalPool
    by_method = {node.split("::")[-1]: rows for node, rows in payload["tests"].items()}
    reverse = {node.split("::")[-1]: node for node in payload["tests"]}

    class Result(unittest.TestResult):
        def addSuccess(self, case):
            super().addSuccess(case)
            passed.append(reverse[case._testMethodName])

    passed = []
    result = Result()
    unittest.TestSuite(Selected(method) for method in by_method).run(result)
    if any(not isinstance(case, unittest.TestCase) for case, _ in result.errors):
        return []
    return passed


def refine(patch, impact, initial_ids, base, cost=None):
    tests = needed(patch, impact, initial_ids)
    if not tests:
        return set()
    if cost is not None:
        weights = index()["rows"]
        seconds = sum(weights[node][1][i] for node, indices in tests.items() for i in indices) / 1_000_000
        saving = cost(initial_ids) - cost(set(initial_ids).difference(tests))
        if .65 + seconds > .7 * saving:
            return set()
    from patchrun import run
    source = (Path(base) / patch.path).read_text()
    result = run(base, {patch.path: patch.apply(source)}, "executor_queries", {"tests": tests}, timeout=7.0)
    return set(tests).intersection(result) if isinstance(result, list) else set()
