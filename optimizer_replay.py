"""Replay pinned parser and optimizer calls to discharge expensive candidate tests."""

import ast
import hashlib
import json
import importlib
import zlib
from functools import lru_cache
from pathlib import Path

MAPS = Path(__file__).resolve().parent / "maps"
TARGETS = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "tests/test_optimizer.py::TestOptimizer::test_tpcds",
    "tests/test_optimizer.py::TestOptimizer::test_tpch",
)
PREFIXES = tuple("sqlglot/" + name + "/" for name in (
    "optimizer", "parsers", "generators", "typing", "dialects",
))


def fingerprint(value):
    if isinstance(value, list):
        payload = value
    else:
        # The upstream optimizer tests check these parent links as well as SQL.
        for parent in value.walk():
            for child in parent.iter_expressions():
                key = child.arg_key
                if child.parent is not parent or key not in parent.args:
                    raise ValueError("Broken AST parent link")
                item = parent.args[key]
                if isinstance(item, list):
                    if child.index is None or item[child.index] is not child:
                        raise ValueError("Broken AST child index")
                elif item is not child:
                    raise ValueError("Broken AST argument")
        payload = value.dump()
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


@lru_cache(maxsize=1)
def coverage_index():
    from mapdata import load

    return load(MAPS / "optimizer_replay.json.gz")


def handle(payload):
    """Called in a separate interpreter with the patched checkout on its path."""
    from sqlglot import exp, parse_one, transpile
    from sqlglot.optimizer import optimize

    index = coverage_index()
    records = index["records"]
    parents = {}
    parent = None
    for i, record in enumerate(records):
        method = record[0]
        if method.startswith("rule:"):
            parents[i] = parent
        else:
            parent = i if method == "optimize" else None

    unchanged = []
    outcomes = {}
    with (MAPS / "optimizer_replay.bin").open("rb") as stream:
        def matches(i):
            if i in outcomes:
                return outcomes[i]
            method, offset, length, options, previous = records[i]
            try:
                stream.seek(offset)
                data = json.loads(zlib.decompress(stream.read(length)))
                if method.startswith("rule:"):
                    module, name = method[5:].rsplit(".", 1)
                    fn = getattr(importlib.import_module(module), name)
                    result = fn(exp.Expr.load(data), **options)
                elif method == "optimize":
                    result = optimize(exp.Expr.load(data), **options)
                elif method == "parse_one":
                    result = parse_one(data, **options)
                elif method == "transpile":
                    result = transpile(data, **options)
                else:
                    raise ValueError("Unknown capture kind")
                outcomes[i] = fingerprint(result) == previous
            except Exception:
                outcomes[i] = False
            return outcomes[i]

        for test, ids in payload["tests"].items():
            for i in ids:
                if matches(i):
                    continue
                # A rule can produce a different intermediate tree that later
                # optimizer rules normalize. Compare the full query in that case.
                outer = parents.get(i)
                if outer is None or not matches(outer):
                    break
            else:
                unchanged.append(test)
    return unchanged


def _indices(mask):
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def _literal_replacement(patch):
    values = {}
    for prefix in ("-", "+"):
        group = [line[1:].strip() for hunk in patch.hunks for line in hunk.body
                 if line.startswith(prefix)]
        if len(group) != 1:
            return False
        try:
            node, = ast.parse(group[0]).body
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                return False
            target = node.targets[0]
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError):
            return False
        if not isinstance(target, ast.Name) or not isinstance(value, (int, float, str)):
            return False
        values[prefix] = (target.id, type(value))
    return values["-"] == values["+"]


def required_calls(patch, impact, candidates):
    """Map candidate test names to the calls whose baseline path reached the edit."""
    path = patch.path
    if not path.startswith(PREFIXES) or impact.invalid or impact.equivalent:
        return {}
    if patch.old_path != patch.new_path or not candidates.intersection(TARGETS):
        return {}

    # For an initializer edit, line coverage cannot identify reads of an imported
    # binding. The caller may use it outside any captured function.
    idx = coverage_index()
    isolated = idx["private"].get(path, ())
    if impact.global_change and not (
        impact.changed_names and set(isolated).issuperset(impact.changed_names)
        and _literal_replacement(patch)
    ):
        return {}
    contexts = idx["coverage"].get(path)
    if not contexts or not impact.lines:
        return {}
    touched = 0
    lines = contexts if impact.global_change else impact.lines
    for line in lines:
        pos = contexts.get(str(line))
        if pos is not None:
            touched |= int(idx["pool"][pos], 16)
    result = {}
    for i, test in enumerate(TARGETS):
        if test not in candidates or test not in idx["valid"] or touched & (1 << i):
            continue
        calls = int(idx["calls"][test], 16)
        covered = touched & calls
        if covered:
            result[test] = list(_indices(covered >> len(TARGETS)))
    return result


def refine(patch, impact, initial_ids, base):
    needed = required_calls(patch, impact, set(initial_ids))
    if not needed:
        return set()
    from patchrun import run

    path = patch.path
    new = patch.apply((Path(base) / path).read_text(encoding="utf-8"))
    result = run(Path(base), {path: new}, "optimizer_replay", {"tests": needed}, timeout=9.0)
    if not isinstance(result, list):
        return set()
    return set(result).intersection(needed)
