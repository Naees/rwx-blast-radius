"""Select tests whose observed integer-branch bounds a scalar change crosses."""

import ast
from functools import lru_cache
from pathlib import Path

from configmap import literal, members, module_members, module_name
from impact import source_window
from mapdata import load


@lru_cache(1)
def profile():
    path = Path(__file__).parent / "maps/numeric.json.gz"
    return load(path) if path.is_file() else None


def select(patch, source, deps):
    data = profile()
    if not data or patch.path not in data["paths"]:
        return None
    module = module_name(patch.path)
    old_text, new_text = source_window(patch, source, patch.apply(source))
    old, new = ast.parse(old_text), ast.parse(new_text)
    original = dict([*module_members(old, module), *members(old, module)])
    changed = dict([*module_members(new, module), *members(new, module)])
    if original.keys() != changed.keys():
        return None
    symbol = None
    for name, before in original.items():
        if not any(
            before.lineno <= n <= before.end_lineno for n in patch.removed_lines
        ):
            continue
        after = changed[name]
        new_value = literal(after.value)
        if (
            name not in data["symbols"]
            or type(new_value) is not int
            or symbol is not None
        ):
            return None
        baseline = data["symbols"][name]["base"]
        if literal(before.value) != baseline:
            return None
        symbol, delta = name, new_value - baseline
        after.value = before.value
    if symbol is None or ast.dump(old) != ast.dump(new):
        return None
    hits = {
        data["tests"][int(i)]
        for i, (lo, hi) in data["symbols"][symbol]["bounds"].items()
        if (lo is not None and delta < lo) or (hi is not None and delta > hi)
    }
    return hits | (set(data["incomplete"]) & deps.file(patch.path))
