"""Evaluate short-circuit operator edits against baseline truth vectors."""

import ast
from functools import lru_cache
from pathlib import Path

from mapdata import load


def tree(node, leaves):
    if isinstance(node, ast.BoolOp):
        return (isinstance(node.op, ast.And), [tree(child, leaves) for child in node.values])
    leaves.append(node)
    return len(leaves) - 1


def result(expr, vector):
    if isinstance(expr, int):
        return {"T": True, "F": False, "?": None}[vector[expr]]
    conjunction, children = expr
    for child in children:
        value = result(child, vector)
        if value is None or value != conjunction:
            return value
    return conjunction


def location(path, node):
    return f"{path}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"


@lru_cache(1)
def profile():
    path = Path(__file__).parent / "maps/bool_values.json.gz"
    return load(path) if path.is_file() else None


def affected(path, before, after, candidates=None):
    if not isinstance(before, ast.BoolOp) or not isinstance(after, ast.BoolOp):
        return None
    old_leaves, new_leaves = [], []
    old, new = tree(before, old_leaves), tree(after, new_leaves)
    if old == new or [ast.dump(n) for n in old_leaves] != [ast.dump(n) for n in new_leaves]:
        return None
    data = profile()
    if not data:
        return None
    site = data["sites"].get(location(path, before))
    if site is None:
        return None
    opaque, vectors = site
    mask = int(data["pool"][opaque], 16)
    for vector, index in vectors.items():
        a, b = result(old, vector), result(new, vector)
        # A reachable unknown could change the value, raise, or produce a side
        # effect. Only sequences of proven-pure evaluations may be omitted.
        if a is None or b is None or a != b:
            mask |= int(data["pool"][index], 16)
    hits = set(data["incomplete"])
    if candidates is not None:
        hits.intersection_update(candidates)
    while mask:
        bit = mask & -mask
        hits.add(data["tests"][bit.bit_length()-1])
        mask ^= bit
    return hits
