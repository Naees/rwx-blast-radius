"""Spend a bounded amount of CPU verifying candidates when it can reduce CI cost."""

import assertion_replay
import query_checks
from patchrun import run


def refine(patch, impact, candidates, selector):
    return _verify([patch], impact, candidates, selector)


def refine_many(patches, candidates, selector):
    if not any(p.path.startswith("sqlglot/") for p in patches):
        return set()
    if any(p.binary or p.old_path != p.new_path or not (
        (p.path.startswith("sqlglot/") and p.path.endswith(".py"))
        or p.path.startswith("tests/fixtures/")
    ) for p in patches):
        return set()
    if len({p.path for p in patches}) != len(patches):
        return set()
    # Run complete candidate methods against all edits together.
    return _verify(patches, None, candidates, selector)


def _verify(patches, impact, candidates, selector):
    if not candidates:
        return set()
    costs = {n: selector.inventory[n]["seconds"] for n in candidates}
    eligible = {n for n in candidates if n.count("::") == 2 and "[" not in n
                and (costs[n] <= 1.5 or (n.startswith("tests/test_optimizer.py::") and costs[n] <= 2.6))}
    if not eligible:
        return set()
    full = selector.cost(candidates)
    db_nodes = {n for n in eligible if n.startswith(("tests/test_optimizer.py", "tests/test_executor.py"))}
    if db_nodes:
        # These classes create and populate a database before a single method
        # runs. A smaller schedule cannot amortize a fresh database and imports.
        removable = full - selector.cost(candidates - eligible)
        if removable < 4.75:
            eligible -= db_nodes
            if not eligible:
                return set()
    dialects = {n for n in eligible if n.startswith("tests/dialects/")}
    optimizers = {n for n in eligible if n.startswith("tests/test_optimizer.py::")}
    assertions, fixtures, ratios = {}, {}, {}
    patch = patches[0]
    slice_tests = (impact is not None and not impact.global_change
                   and not selector.deps.import_initializers(patch.path, impact.lines))
    if dialects and slice_tests:
        assertions, r = assertion_replay.ranges_to_skip(impact, patch.path, dialects)
        ratios.update(r)
    if optimizers and slice_tests:
        fixtures, r = query_checks.plan(patch, impact, optimizers)
        ratios.update(r)
    if impact is not None and impact.global_change and (dialects or optimizers):
        from config_sites import plans
        assertions, fixtures, ratios = plans(patch, impact, selector, dialects, optimizers)
    runtime = {n: costs[n] * ratios.get(n, 1.0) for n in eligible}
    def probe_budget(nodes):
        fixed = candidates - nodes
        # Compare two outcomes: all candidates pass, or the most expensive
        # candidate still needs a worker. The latter is common on breaking edits.
        clear_all = full - selector.cost(fixed)
        longest = max(nodes, key=lambda n: (costs[n], n))
        keep_one = max(0.0, full - selector.cost(fixed | {longest}))
        return min(3.0, .35 * clear_all + .35 * keep_one)

    def worthwhile(nodes):
        # DB imports and class fixtures have a measurable fixed cost.
        setup = 1.25 if db_nodes.intersection(nodes) else .15
        return setup + sum(runtime[n] for n in nodes) < probe_budget(nodes)

    if not worthwhile(eligible):
        # A few slow but cheaply replayable tests can still dominate a worker's load.
        eligible = {n for n in eligible if costs[n] >= .20 and runtime[n] < .75 * costs[n]}
        if not eligible or not worthwhile(eligible):
            return set()
    # Keep class fixtures together. Within each class, prioritize tests that
    # cover the largest scheduled duration per estimated second of verification.
    groups = {}
    for name in eligible:
        groups.setdefault(name.rsplit("::", 1)[0], []).append(name)
    def priority(names):
        return -sum(costs[n] for n in names) / (.04 + sum(runtime[n] for n in names))
    nodes = [n for group in sorted(groups, key=lambda g: (priority(groups[g]), g))
             for n in sorted(groups[group], key=lambda n: (priority([n]), n))]
    payload = {
        "nodes": nodes,
        "assertions": {n: assertions[n] for n in nodes if assertions.get(n)},
        "fixtures": {n: fixtures[n] for n in nodes if fixtures.get(n)},
        "stop_early": len(nodes) <= 8 and sum(costs[n] for n in nodes) < .4,
        "cpu_budget": probe_budget(eligible),
    }
    changed = {p.path: p.apply(selector.source(p.path)) for p in patches}
    result = run(selector.base, changed, "query_checks", payload, timeout=8.0)
    return eligible.intersection(result) if isinstance(result, list) else set()
