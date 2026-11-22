"""Map an isolated scalar assignment to its checked parser/generator predicate sites."""
import ast
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from configmap import literal, members, module_members, module_name
from impact import source_window
from mapdata import load


@lru_cache(maxsize=1)
def index():
    path = Path(__file__).resolve().parent / "maps/config_sites.json.gz"
    if not path.is_file():
        return {}, frozenset()
    result = load(path)
    return result["sites"], frozenset(result["tests"])


def context(patch, impact, source, deps):
    if not impact.global_change or patch.old_path != patch.new_path or len(patch.removed_lines) != 1 or len(patch.added_lines) != 1:
        return None
    sites, measured = index()
    module = module_name(patch.path)
    if len(impact.changed_names) != 1:
        return None
    name, = impact.changed_names
    expected = ".".join([module, *impact.classes, name])
    if expected not in sites:
        return None
    old, new = source_window(patch, source, patch.apply(source))
    before, after = ast.parse(old), ast.parse(new)
    definitions = dict([*members(before, module), *module_members(before, module)])
    updates = dict([*members(after, module), *module_members(after, module)])
    if definitions.keys() != updates.keys():
        return None
    symbol = None
    for name, prior in definitions.items():
        changed = updates[name]
        if ast.dump(prior.value) == ast.dump(changed.value):
            continue
        if (name != expected or symbol is not None or type(literal(prior.value)) not in {int, str, float, bool}
                or type(literal(prior.value)) is not type(literal(changed.value))
                or prior.lineno not in patch.removed_lines or changed.lineno not in patch.added_lines):
            return None
        symbol = name
        changed.value = prior.value
    if symbol is None or ast.dump(before) != ast.dump(after):
        return None
    path, rows = sites[symbol]
    if deps.import_initializers(path, rows):
        return None
    # The normal row/validator maps retain their shared setup and coverage guards.
    return path, replace(impact, global_change=False, lines=set(rows)), measured


def plans(patch, impact, selector, dialects, optimizers):
    """Generate the same partial-probe plans as a directly edited consumer line."""
    from types import SimpleNamespace
    import assertion_replay
    import query_checks

    result = context(patch, impact, selector.source(patch.path), selector.deps)
    if result is None:
        return {}, {}, {}
    path, predicate, measured = result
    dialects = set(dialects).intersection(measured)
    optimizers = set(optimizers).intersection(measured)
    assertions, fixtures, ratios = {}, {}, {}
    if dialects:
        assertions, target = assertion_replay.ranges_to_skip(predicate, path, dialects)
        ratios.update(target)
    if optimizers:
        fixtures, target = query_checks.plan(SimpleNamespace(path=path), predicate, optimizers)
        ratios.update(target)
    return assertions, fixtures, ratios
