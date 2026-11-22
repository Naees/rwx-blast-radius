"""Replay affected SQL fixture pairs in SQLGlot's optimizer tests."""

import concurrent.futures
import sys
from functools import lru_cache
from multiprocessing.reduction import ForkingPickler
from pathlib import Path


class LocalPool:
    """Preserve worker input/output serialization while running one job at a time."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            fn, args = self.transfer((initializer, initargs))
            fn(*args)

    @staticmethod
    def transfer(value):
        return ForkingPickler.loads(ForkingPickler.dumps(value))

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            func, args, kwargs = self.transfer((fn, args, kwargs))
            f.set_result(self.transfer(func(*args, **kwargs)))
        except Exception as exc:
            f.set_exception(exc)
        return f

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def shutdown(self, *_, **__):
        pass


@lru_cache(maxsize=1)
def index():
    from mapdata import load
    raw = load(Path(__file__).parent / "maps/queries.json.gz")
    groups = {}
    outside = {name: 1 << i for i, name in enumerate(raw.get("outside_tests", ()))}
    for bit, (node, path, row) in enumerate(raw["cases"], start=len(outside)):
        groups.setdefault(node, {}).setdefault(path, []).append((bit, row))
    return groups, raw["lines"], [int(mask, 16) for mask in raw["pool"]], outside


def plan(patch, impact, nodes):
    groups, lines, pool, outside = index()
    table = lines.get(patch.path, {})
    touched = 0
    for line in impact.lines:
        pos = table.get(str(line))
        if pos is not None:
            touched |= pool[pos]
    plans, ratios = {}, {}
    for node in nodes:
        files = {}
        total = kept = 0
        # A helper run before or after the row can change the schema, dialect,
        # or options shared by all rows. A partial probe cannot verify that use.
        if node not in outside or touched & outside[node]:
            plans[node], ratios[node] = {}, 1.0
            continue
        for path, entries in groups.get(node, {}).items():
            selected = [row for i, row in entries if touched & (1 << i)]
            if not impact.global_change:
                files[path] = selected
            total += len(entries)
            kept += len(selected)
        if not kept:
            files = {}
        fraction = (kept + 1) / (total + 1) if files else 1
        ratios[node] = .15 + .85 * fraction
        plans[node] = files
    return plans, ratios


def handle(payload):
    import importlib
    import signal
    import time
    import unittest
    import assertion_replay
    start = time.process_time()
    if payload["assertions"]:
        assertion_replay.install(payload["assertions"])
    concurrent.futures.ProcessPoolExecutor = LocalPool
    cases = {}
    for name in payload["nodes"]:
        path, cls, method = name.split("::")
        try:
            mod = importlib.import_module(path[:-3].replace("/", "."))
            cases[name] = getattr(mod, cls)(method)
        except (Exception, SystemExit):
            pass

    class Result(unittest.TestResult):
        active = None

        def __init__(self):
            super().__init__()
            self.passed = set()
            self.failfast = payload["stop_early"]

        def startTest(self, case):
            self.active = by_id.get(case.id())
            super().startTest(case)

        def stopTest(self, case):
            self.active = None
            super().stopTest(case)

        def addSuccess(self, case):
            self.passed.add(by_id[case.id()])

    by_id = {case.id(): name for name, case in cases.items()}
    result = Result()
    optimizers = [case for name, case in cases.items() if name.startswith("tests/test_optimizer.py::")]
    if optimizers:
        mod = importlib.import_module("tests.test_optimizer")
        original = mod.load_sql_fixture_pairs

        def selected(path):
            keep = None
            caller = sys._getframe(1).f_code
            if caller.co_name in {"check_file", "test_annotate_funcs", "test_annotate_types"}:
                keep = payload["fixtures"].get(result.active, {}).get(path)
            for i, pair in enumerate(original(path)):
                if keep is None or i in keep:
                    yield pair
        mod.load_sql_fixture_pairs = selected
    # Finish with the tests already verified if the remaining potential
    # scheduling gain no longer justifies more CPU. The parent also enforces
    # a wall timeout for blocked native calls.
    remaining = payload.get("cpu_budget", 8.0) - (time.process_time() - start)
    if remaining <= 0:
        return []
    verified = None
    def exhausted(*_):
        nonlocal verified
        verified = result.passed.copy() if result.active is not None else set()
        verified.discard(result.active)
        result.stop()
        raise TimeoutError("Verification CPU budget exhausted")
    previous = signal.signal(signal.SIGPROF, exhausted)
    try:
        signal.setitimer(signal.ITIMER_PROF, remaining)
        unittest.TestSuite(cases.values()).run(result)
    except TimeoutError:
        if verified is None:
            return []
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, previous)
    if any(not isinstance(case, unittest.TestCase) for case, _ in result.errors):
        return []
    return sorted(result.passed if verified is None else verified)
