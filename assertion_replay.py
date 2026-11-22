"""Run candidate dialect tests, skipping independent assertions untouched by a patch."""

from functools import lru_cache
from pathlib import Path

from mapdata import load


@lru_cache(maxsize=1)
def index():
    raw = load(Path(__file__).parent / "maps/assertions.json.gz")
    by_test = {}
    known = int(raw["known"], 16)
    for i, (node, start, end) in enumerate(raw["steps"]):
        if known & (1 << i):
            by_test.setdefault(node, []).append((i, start, end))
    outside = {name: 1 << (i + len(raw["steps"]))
               for i, name in enumerate(raw.get("outside_tests", ()))}
    return by_test, [int(v, 16) for v in raw["pool"]], raw["lines"], outside


def ranges_to_skip(impact, path, names):
    by_test, pool, lines, outside = index()
    touched = 0
    table = lines.get(path, {})
    for line in impact.lines:
        pos = table.get(str(line))
        if pos is not None:
            touched |= pool[pos]
    ranges, ratios = {}, {}
    for name in names:
        steps = by_test.get(name, ())
        unused = [(a, b) for i, a, b in steps if not touched & (1 << i)]
        if name not in outside or touched & outside[name] or len(unused) == len(steps):
            unused = []
        ranges[name] = unused
        # Reserve time for the rest of the method, including arbitrary assertions.
        ratios[name] = 1 - .75 * len(unused) / len(steps) if steps else 1
    return ranges, ratios


def install(ranges):
    import functools
    import sys
    from tests.dialects.test_dialect import Validator

    skip = set()
    for node, intervals in ranges.items():
        path, cls, method = node.split("::")
        filename = str(Path.cwd() / path)
        for a, b in intervals:
            for line in range(a, b + 1):
                skip.add((filename, cls + "." + method, line))

    def filtered(fn):
        @functools.wraps(fn)
        def invoke(self, *args, **kwargs):
            caller = sys._getframe(1)
            code = caller.f_code
            if (code.co_filename, code.co_qualname, caller.f_lineno) in skip:
                return None
            return fn(self, *args, **kwargs)
        return invoke

    if skip:
        for name in ("validate_all", "validate_transpile", "validate_identity"):
            setattr(Validator, name, filtered(getattr(Validator, name)))
