"""Resolve value-only changes in source dictionaries from baseline key reads."""

import ast
from functools import lru_cache
from pathlib import Path

from configmap import members, module_members, module_name
from impact import source_window
from mapdata import load


@lru_cache(1)
def profile():
    path = Path(__file__).parent / "maps/registries.json.gz"
    return load(path) if path.is_file() else None


def _same(a, b):
    if a is None or b is None:
        return a is b
    return ast.dump(a, annotate_fields=False) == ast.dump(b, annotate_fields=False)


def _safe_value(a, b, names, pool, factories):
    """Replacing a stored value must have no untracked definition-time effects."""
    if _same(a, b):
        return True
    if isinstance(a, ast.Constant) and isinstance(b, ast.Constant):
        return type(a.value) is type(b.value)
    if isinstance(a, ast.Lambda) and isinstance(b, ast.Lambda):
        return _same(a.args, b.args)
    if isinstance(a, ast.Attribute) and isinstance(b, ast.Attribute):
        prefix = ast.unparse(a.value)
        return (
            _same(a.value, b.value)
            and prefix in names
            and b.attr in pool[names[prefix]]
        )
    if isinstance(a, ast.Dict) and isinstance(b, ast.Dict):
        return (
            len(a.keys) == len(b.keys)
            and all(_same(k, n) for k, n in zip(a.keys, b.keys))
            and all(
                _safe_value(x, y, names, pool, factories)
                for x, y in zip(a.values, b.values)
            )
        )
    if type(a) is type(b) and isinstance(a, (ast.List, ast.Tuple)):
        return len(a.elts) == len(b.elts) and all(
            _safe_value(x, y, names, pool, factories) for x, y in zip(a.elts, b.elts)
        )
    if isinstance(a, ast.Call) and isinstance(b, ast.Call):
        if (
            not _same(a.func, b.func)
            or ast.unparse(a.func) not in factories
            or len(a.args) != len(b.args)
            or [kw.arg for kw in a.keywords] != [kw.arg for kw in b.keywords]
        ):
            return False
        values = [
            *zip(a.args, b.args),
            *zip((k.value for k in a.keywords), (k.value for k in b.keywords)),
        ]
        return all(_safe_value(x, y, names, pool, factories) for x, y in values)
    return False


def _sites(old, new, names, pool, factories):
    if isinstance(old, ast.DictComp) and isinstance(new, ast.DictComp):
        if (
            not _same(old.key, new.key)
            or len(old.generators) != len(new.generators)
            or not all(_same(a, b) for a, b in zip(old.generators, new.generators))
            or not _safe_value(old.value, new.value, names, pool, factories)
        ):
            return None
        return [old.value]
    if (
        not isinstance(old, ast.Dict)
        or not isinstance(new, ast.Dict)
        or len(old.keys) != len(new.keys)
    ):
        return None
    changed = []
    for k, n, a, b in zip(old.keys, new.keys, old.values, new.values):
        if not _same(k, n):
            return None
        if _same(a, b):
            continue
        if k is None:
            if not isinstance(a, ast.DictComp):
                return None
            nested = _sites(a, b, names, pool, factories)
            if nested is None:
                return None
            changed.extend(nested)
        elif _safe_value(a, b, names, pool, factories):
            changed.append(a)
        else:
            return None
    return changed


def select(patch, source, deps):
    data = profile()
    if not data or patch.path not in data["files"] or patch.old_path != patch.new_path:
        return None
    sites = data["files"][patch.path]
    removed = patch.removed_lines
    if not removed:
        return None
    # Index whole value ranges: multi-line lambdas can be edited below their
    # first line. Most method-body diffs never touch any dictionary entry.
    candidates = data["ranges"][patch.path]
    if not any(lo <= i <= hi for i in removed for lo, hi in candidates):
        return None
    baseline, updated = source_window(patch, source, patch.apply(source))
    old, new = ast.parse(baseline), ast.parse(updated)
    module = module_name(patch.path)
    old_defs = dict([*members(old, module), *module_members(old, module)])
    new_defs = dict([*members(new, module), *module_members(new, module)])
    if old_defs.keys() != new_defs.keys():
        return None
    mask = 0
    found = False
    for symbol, before in old_defs.items():
        after = new_defs[symbol]
        if not any(before.lineno <= i <= before.end_lineno for i in removed) or _same(
            before.value, after.value
        ):
            continue
        differences = _sites(
            before.value,
            after.value,
            data["names"].get(module, {}),
            data["pool_names"],
            data.get("factories", {}).get(module, ()),
        )
        if not differences:
            return None
        for v in differences:
            entry = sites.get(f"{v.lineno}:{v.col_offset}")
            if entry is None:
                return None
            mask |= int(data["pool"][entry], 16)
        after.value = before.value
        found = True
    if not found or not _same(old, new):
        return None
    return {test for i, test in enumerate(data["tests"]) if mask & 1 << i} | (
        set(data["incomplete"]) & deps.file(patch.path)
    )
