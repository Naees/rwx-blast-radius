"""Select and schedule tests for independent patches against the pinned baseline."""

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from configmap import select as select_config
from deps import Dependencies
from fixtures import FixtureSelector
from impact import analyze_python, parse_patch
from mapdata import load as load_map
from numeric import select as numeric_users
from observations import Observations
from registries import select as registry_users


def schedule(tests):
    """Try every legal worker count, placing the longest tests first."""
    items = sorted(((round(t["seconds"] * 1000), t["number"]) for t in tests), reverse=True)
    best, best_cost = [], float("inf")
    for count in range(1, min(8, len(items)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for duration, number in items:
            index = loads.index(min(loads))
            workers[index].append(number)
            loads[index] += duration
        cost = max(loads) + 2000 * count
        if cost < best_cost:
            best, best_cost = [w for w in workers if w], cost
    return best


class Selector:
    def __init__(self, base, tests):
        self.base = Path(base).resolve()
        self.inventory = {row["id"]: row for row in tests}
        self.all = set(self.inventory)
        self.durations = {r["number"]: r["seconds"] for r in tests}
        self.deps = Dependencies()
        self.observations = Observations()
        self.imports = load_map(Path(__file__).parent / "maps/imports.json.gz")
        self.fixtures = FixtureSelector(base, self.all, self.deps)

    @lru_cache(maxsize=64)
    def source(self, path):
        p = (self.base / path).resolve()
        if not p.is_relative_to(self.base):
            raise ValueError("Patch path outside checkout")
        return p.read_text("utf-8")

    def candidates(self, patch):
        path = patch.path
        for filename in (patch.old_path, patch.new_path):
            if filename and (Path(filename).is_absolute() or ".." in Path(filename).parts):
                return self.all
        if patch.binary:
            return self.all
        selected = self.fixtures.select(patch)
        if selected is not None:
            return selected
        if path.endswith(".py") and path.startswith("sqlglot/"):
            source = self.source(patch.old_path) if patch.old_path else ""
            impact = analyze_python(patch, source)
            if impact.equivalent:
                return set()
            if patch.old_path != patch.new_path:
                return self.all
            if impact.invalid:
                return self.import_users(path)
            hits = set().union(*(self.deps.line(path, line) for line in impact.lines))
            initializers = ()
            if not hits and not impact.global_change:
                initializers = self.deps.import_initializers(path, impact.lines)
            registry = registry_users(patch, source, self.deps)
            if impact.global_change:
                users = numeric_users(patch, source, self.deps)
                if users is None:
                    users = select_config(patch, impact, source, self.deps)
                # A fully resolved class assignment runs at import. The per-test
                # dependency is at the consumer, not at the assignment line.
                if registry is not None:
                    hits = registry if users is None else registry.intersection(users)
                else:
                    hits = self.global_users(path) if users is None else users
            elif registry is not None:
                hits = registry
            elif impact.expressions:
                groups = [self.observations.affected(path, old, new) for old, new in impact.expressions]
                if all(group is not None for group in groups):
                    hits = set().union(*groups, hits & self.observations.incomplete)
            for owner in initializers:
                hits.update(self.import_users(owner))
            hits = (hits | set(self.deps.source_readers(path)) | (self.all - self.deps.tests)) & self.all
            return hits
        if path in {"pyproject.toml", "setup.cfg", "conftest.py", "tests/conftest.py"}:
            return self.all
        return self.deps.fixture(path)

    def cost(self, nodes):
        workers = schedule(self.inventory[t] for t in nodes)
        return 2 * len(workers) + max((sum(self.durations[t] for t in w) for w in workers), default=0)

    def import_users(self, path):
        importers = {path, *self.imports["readers"].get(path, ()), *self.imports["unknown"]}
        hits = {name for name in self.all if self.inventory[name]["file"] in importers}
        for user in importers:
            hits.update(self.deps.file(user))
            hits.update(self.deps.fixture(user))
        return hits | (self.all - self.deps.tests)

    def global_users(self, path):
        # Dialect implementations inherit from one another. Include consumers that
        # execute any of this dialect's matching parser, generator, or typing code.
        _, folder, *rest = path.split("/")
        if folder in {"parsers", "generators", "dialects", "typing"} and len(rest) == 1:
            name = Path(rest[0]).stem
            if name not in {"__init__", "base", "dialect"}:
                hits = {test for test in self.all if test.startswith(f"tests/dialects/test_{name}.py::")}
                for component in ("parsers", "generators", "dialects", "typing"):
                    hits.update(self.deps.file(f"sqlglot/{component}/{name}.py"))
                return hits
        return self.all

    def predict(self, text):
        patches = parse_patch(text)
        if not patches:
            return self.all
        hits = set()
        for patch in patches:
            hits.update(self.candidates(patch))
        hits.intersection_update(self.all)
        return hits


def main():
    data = Path(os.environ.get("BLAST_DATA", "/data"))
    base = os.environ.get("BLAST_BASE", "/sqlglot")
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    selector = Selector(base, tests)
    for path in sorted((data / "commits").glob("*.patch")):
        try:
            hits = selector.predict(path.read_text("utf-8"))
        except (ValueError, OSError, KeyError, SyntaxError, IndexError) as exc:
            print(f"{path.stem}: selecting full suite after {type(exc).__name__}", file=sys.stderr)
            hits = selector.all
        workers = schedule(selector.inventory[t] for t in hits)
        print(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
