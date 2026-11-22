"""Select and schedule tests for independent patches against the pinned baseline."""

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

from bool_values import affected as boolean_users
from class_checks import select as class_check_users
from configmap import select as select_config
from default_args import select as default_users
from deps import Dependencies
from fixture_usage import select as fixture_users
from fixtures import FixtureSelector
from impact import analyze_python, parse_patch
from literal_values import affected as literal_users
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

    def candidates(self, patch, allow_probe=False, fixture_batch=False):
        path = patch.path
        for filename in (patch.old_path, patch.new_path):
            if filename and (Path(filename).is_absolute() or ".." in Path(filename).parts):
                return self.all
        if patch.binary:
            return self.all
        if allow_probe or fixture_batch:
            mapped = fixture_users(patch, self.base, self.all, self.deps)
            if mapped is not None:
                return mapped
        selected = self.fixtures.select(patch)
        if selected is not None:
            return selected
        if path.endswith(".py") and path.startswith("sqlglot/"):
            source = self.source(patch.old_path) if patch.old_path else ""
            impact = analyze_python(patch, source)
            if impact.equivalent:
                return set(self.deps.source_readers(path))
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
                if users is None:
                    users = default_users(patch, source, self.base, self.deps)
                # A fully resolved class assignment runs at import. The per-test
                # dependency is at the consumer, not at the assignment line.
                if registry is not None:
                    hits = registry if users is None else registry.intersection(users)
                else:
                    hits = self.global_users(path) if users is None else users
            elif registry is not None:
                hits = registry
            elif impact.expressions:
                groups = []
                for old, new in impact.expressions:
                    group = boolean_users(path, old, new, hits)
                    if group is None:
                        group = self.observations.affected(path, old, new)
                    if group is None:
                        group = literal_users(path, old, new, hits)
                    groups.append(group)
                if all(group is not None for group in groups):
                    hits = set().union(*groups, hits & self.observations.incomplete)
            if hits and not impact.global_change and registry is None:
                checked = class_check_users(patch, source, hits)
                if checked is not None:
                    hits = checked
            for owner in initializers:
                hits.update(self.import_users(owner))
            hits = (hits | set(self.deps.source_readers(path)) | (self.all - self.deps.tests)) & self.all
            if allow_probe and hits:
                import executor_queries
                import optimizer_replay
                import stress
                import verification
                hits -= optimizer_replay.refine(patch, impact, hits, self.base, self.cost)
                hits -= executor_queries.refine(patch, impact, hits, self.base, self.cost)
                hits -= verification.refine(patch, impact, hits, self)
                hits -= stress.refine(patch, impact, hits, self.base, self.inventory)
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

    def _comment_only(self, patch):
        if (patch.binary or patch.old_path != patch.new_path or not patch.path.startswith("sqlglot/")
                or not patch.path.endswith(".py")):
            return False
        changed = (line[1:].strip() for hunk in patch.hunks for line in hunk.body if line[0] in "+-")
        if any(line and not line.startswith("#") for line in changed):
            return False
        # A physical line beginning with '#' can be inside a string literal.
        return analyze_python(patch, self.source(patch.path)).equivalent

    def predict(self, text):
        patches = parse_patch(text)
        if not patches:
            return self.all
        raw_readers = set()
        if len(patches) > 1 and len({p.path for p in patches}) == len(patches):
            active = []
            for patch in patches:
                if self._comment_only(patch):
                    raw_readers.update(self.deps.source_readers(patch.path))
                else:
                    active.append(patch)
            patches = active
        hits = set()
        fixture_batch = all(p.path.startswith("tests/fixtures/") for p in patches)
        for patch in patches:
            hits.update(self.candidates(patch, allow_probe=len(patches) == 1, fixture_batch=fixture_batch))
        hits.intersection_update(self.all)
        if len(patches) > 1 and hits:
            import verification
            hits -= verification.refine_many(patches, hits, self)
        return hits | (raw_readers & self.all)


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
