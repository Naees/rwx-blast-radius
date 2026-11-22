"""Probe individual assertions in two expensive, repetitive tests on patched code."""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch as replace

from mapdata import load


PARSER = "tests/test_parser.py::TestParser::test_max_nodes"
GENERATOR = "tests/test_generator.py::TestGenerator::test_generator_memory"
EXPENSIVE = frozenset({PARSER, GENERATOR})
MAP = Path(__file__).parent / "maps/stress.json.gz"

# A shorter sequence cannot establish behavior conditional on a large input,
# an iteration count, or accumulating allocations.
_SCALE = re.compile(
    r"\b(?:while|for|yield|range|len|enumerate|zip|sum|min|max|reversed|sort|sorted|"
    r"append|extend|insert|copy|clear|gc|tracemalloc|recursion|pop|setdefault)\b|"
    r"\b\w*(?:count|limit|size|index|offset|depth|width|capacity|cache|memory|length|buffer|"
    r"timeout|budget|position|threshold|elapsed)\w*\b|\b\d[\d_]{2,}", re.I,
)


@lru_cache(maxsize=1)
def _map():
    return load(MAP)


def repeat_sql(sql, width):
    # The expensive input is a SELECT whose columns are identical. Verify that
    # property on the actual test input before shortening it.
    prefix = "SELECT "
    if not sql.startswith(prefix):
        raise ValueError("Unexpected stress statement")
    columns = sql[len(prefix):].split(",")
    if len(columns) < width or any(col != columns[0] for col in columns):
        raise ValueError("Expected repeated identical columns")
    return prefix + ",".join([columns[0]] * width)


def evidence(sql):
    """Measure the AST and parser node count for one shortened input."""
    from sqlglot import Parser, parse_one
    validate = Parser.validate_expression
    count = 0
    def track(parser, expression, *args, **kwargs):
        nonlocal count
        count += 1
        return validate(parser, expression, *args, **kwargs)
    with replace.object(Parser, "validate_expression", track):
        tree = parse_one(sql, max_nodes=1_000_000)
    encoded = json.dumps(tree.dump(), sort_keys=True, separators=(",", ":")).encode()
    return [count, hashlib.sha256(encoded).hexdigest()]


def _parser(expected):
    from sqlglot import Parser
    from tests import test_parser
    original_parse = test_parser.parse_one
    validate = Parser.validate_expression
    observed = set()
    cache = {}
    def parse(sql, *args, **kwargs):
        if len(sql) < 100_000:
            return original_parse(sql, *args, **kwargs)

        shortened = repeat_sql(sql, 8)
        if shortened not in cache:
            measured = [evidence(repeat_sql(sql, k)) for k in (2, 3, 8)]
            if measured != expected:
                raise AssertionError("Changed AST or node counts in the repeating input")
            step = measured[1][0] - measured[0][0]
            if step <= 0 or measured[2][0] - measured[1][0] != 5 * step:
                raise AssertionError("Node counts do not repeat by column")
            cache[shortened] = step
        step = cache[shortened]
        limit = kwargs["max_nodes"]
        # Skip a whole number of repeated columns; this preserves the node
        # position within the failing column and uses the test's original bound.
        columns = sql.count(",") + 1
        delta = min(columns - 8, limit // step - 4) * step
        if delta <= 0:
            raise AssertionError("Unexpected node limit")
        shifted = False
        def advance(parser, expression, *a, **kw):
            nonlocal shifted
            if not shifted and parser._node_count >= step:
                parser._node_count += delta
                shifted = True
                observed.add(limit)
            return validate(parser, expression, *a, **kw)
        with replace.object(Parser, "validate_expression", advance):
            return original_parse(shortened, *args, **kwargs)

    with replace.object(test_parser, "parse_one", parse):
        test_parser.TestParser("test_max_nodes").test_max_nodes()
    return len(observed) == 2


def _generator(names):
    from tests import test_generator
    if names == "all":
        # Each tracemalloc window begins after the test's own warmup and full
        # collection. Exclude those older live objects from later GC scans;
        # objects allocated in the measurement window still receive full GC.
        import gc
        import tracemalloc
        start = tracemalloc.start
        def measure():
            gc.freeze()
            start()
        try:
            with replace.object(tracemalloc, "start", measure):
                test_generator.TestGenerator("test_generator_memory").test_generator_memory()
        finally:
            gc.unfreeze()
        return True
    dialects = [dialect for dialect in test_generator.Dialects if dialect.value in names]
    if len(dialects) != len(names) or not dialects:
        return False
    # Each original subTest performs its original warmup, collections,
    # tracemalloc checks, and measured iteration count.
    with replace.object(test_generator, "Dialects", dialects):
        test_generator.TestGenerator("test_generator_memory").test_generator_memory()
    return True


def handle(payload):
    passed = []
    if payload.get("parser"):
        try:
            if _parser(payload["parser"]):
                passed.append(PARSER)
        except Exception:
            pass
    if payload.get("generator"):
        try:
            if _generator(payload["generator"]):
                passed.append(GENERATOR)
        except Exception:
            pass
    return passed


def _edited_text(patch):
    return "\n".join(line[1:] for hunk in patch.hunks for line in hunk.body if line[:1] in "+-")


def plan(patch, impact, initial_ids):
    if not EXPENSIVE.intersection(initial_ids) or not patch.path.startswith("sqlglot/"):
        return {}
    if (patch.old_path != patch.new_path or not patch.path.endswith(".py")
            or impact.invalid or impact.equivalent):
        return {}
    data = _map()
    lines = data["files"].get(patch.path, {})
    mask = 0
    for number in impact.lines:
        mask |= lines.get(str(number), 0)
    phases = {name for i, name in enumerate(data["phases"]) if mask & (1 << i)}
    result = {}
    changes = _edited_text(patch)
    if PARSER in initial_ids and not _SCALE.search(changes):
        if phases & {"parse:small", "parse:stress"} or impact.global_change:
            result["parser"] = data["evidence"]
    if GENERATOR in initial_ids:
        names = sorted(p[10:] for p in phases if p.startswith("generator:"))
        if 1 <= len(names) <= 5 and not impact.global_change:
            result["generator"] = names
        elif not re.search(r"\b(?:gc|tracemalloc|weakref|finalize|__del__|getrefcount)\b", changes):
            result["generator"] = "all"
    return result


def _budget(payload, ids, inventory):
    # Compare the best worker cost after removing one or both expensive tests.
    # CPU estimates include startup measured on the local amd64 image.
    from selector import schedule
    keys = {"parser": PARSER, "generator": GENERATOR}
    times = {"parser": 3.3, "generator": 5.6 if payload.get("generator") == "all" else 2.3}
    rows = {inventory[test]["number"]: inventory[test] for test in ids}

    def cost(tests):
        workers = schedule(inventory[test] for test in tests)
        loads = [sum(rows[n]["seconds"] for n in worker) for worker in workers]
        return max(loads, default=0) + 2 * len(workers)

    initial = cost(ids)
    best_gain, best = 0, {}
    choices = [[key] for key in payload]
    if len(payload) > 1:
        choices.append(list(payload))
    for choice in choices:
        remaining = ids.difference(keys[k] for k in choice)
        gain = initial - cost(remaining) - sum(times[k] for k in choice)
        if gain > best_gain:
            best_gain = gain
            best = {k: payload[k] for k in choice}
    return best


def refine(patch, impact, initial_ids, base, inventory=None):
    """Return IDs removed after a successful isolated, patched-code probe."""
    payload = plan(patch, impact, initial_ids)
    if payload and inventory is not None:
        payload = _budget(payload, initial_ids, inventory)
    if not payload:
        return set()
    from patchrun import run
    source = (Path(base) / patch.path).read_text(encoding="utf-8")
    changes = {patch.path: patch.apply(source)}
    result = run(base, changes, "stress", payload, timeout=8)
    if not isinstance(result, list):
        return set()
    return EXPENSIVE.intersection(initial_ids, result)
