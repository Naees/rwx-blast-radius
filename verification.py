"""Spend a bounded amount of CPU verifying candidates when it can reduce CI cost."""

import assertion_replay
import query_checks
from patchrun import run


def refine(patch, impact, candidates, selector):
    return _verify([patch], impact, candidates, selector)


def refine_many(patches, candidates, selector):
    if any(p.binary or p.old_path != p.new_path or not p.path.startswith("sqlglot/")
           or not p.path.endswith(".py") for p in patches):
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
    full = selector.cost(candidates)
    # Starting the database-backed test classes costs more than their recorded
    # per-method durations. Reserve that work for bigger schedules or for
    # cases where coverage removed most of the expensive fixture rows.
    db_nodes = {n for n in eligible if n.startswith(("tests/test_optimizer.py", "tests/test_executor.py"))}
    if full < 4.75 and db_nodes and sum(runtime[n] for n in db_nodes) > .35:
        eligible -= db_nodes
        if not eligible:
            return set()

    def worthwhile(nodes):
        saving = full - selector.cost(candidates - nodes)
        # DB imports and class fixtures have a measurable fixed cost.
        setup = .35 if any(n.startswith(("tests/test_optimizer", "tests/test_executor")) for n in nodes) else .12
        return setup + sum(runtime[n] for n in nodes) < min(5, .8 * saving)

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
    saving = full - selector.cost(candidates - eligible)
    payload = {
        "nodes": nodes,
        "assertions": {n: assertions[n] for n in nodes if assertions.get(n)},
        "fixtures": {n: fixtures[n] for n in nodes if fixtures.get(n)},
        "stop_early": len(nodes) <= 8 and sum(costs[n] for n in nodes) < .4,
        "cpu_budget": min(6.0, .8 * saving),
    }
    changed = {p.path: p.apply(selector.source(p.path)) for p in patches}
    result = run(selector.base, changed, "query_checks", payload, timeout=8.0)
    return eligible.intersection(result) if isinstance(result, list) else set()
