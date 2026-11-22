"""Compare old and new literals with primitive operands observed on the pinned base."""

import ast
import math
import operator
from functools import lru_cache
from pathlib import Path

from mapdata import load


MISSING = object()
OPS = {"Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
       "Gt": operator.gt, "GtE": operator.ge, "In": lambda a, b: a in b,
       "NotIn": lambda a, b: a not in b}
MAP = Path(__file__).parent / "maps/literal_values.json.gz"


def scalar(value):
    kind = type(value)
    if value is None:
        return "n", None
    if kind is bool:
        return "b", value
    if kind is int and value.bit_length() <= 64:
        return "i", value
    if kind is float and math.isfinite(value):
        return "f", value
    if kind is str and len(value) <= 128:
        return "s", value
    return None


def key(value):
    if type(value) is tuple and len(value) <= 8:
        parts = tuple(scalar(item) for item in value)
        return ("t", parts) if None not in parts else None
    return scalar(value)


def value_of(tagged):
    tag, value = tagged
    return tuple(item[1] for item in value) if tag == "t" else value


def constant(node):
    if isinstance(node, ast.Constant):
        return node.value if scalar(node.value) is not None else MISSING
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)):
        value = node.operand.value if isinstance(node.op, ast.UAdd) else -node.operand.value
        return value if scalar(value) is not None else MISSING
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)) and len(node.elts) <= 32:
        values = []
        for el in node.elts:
            if isinstance(el, (ast.List, ast.Set, ast.Tuple)):
                return MISSING
            v = constant(el)
            if v is MISSING:
                return MISSING
            values.append(v)
        if isinstance(node, ast.List):
            return values
        return set(values) if isinstance(node, ast.Set) else tuple(values)
    return MISSING


def shape(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    op = type(node.ops[0]).__name__
    if op not in OPS:
        return None
    l, r = constant(node.left), constant(node.comparators[0])
    if (l is MISSING) == (r is MISSING):
        return None
    if l is MISSING:
        return op, "r", r, node.left
    return op, "l", l, node.comparators[0]


@lru_cache(1)
def profile():
    return load(MAP) if MAP.exists() else None


def affected(path, before, after, candidates=None):
    """Return affected test IDs, or None when the edit needs line-level coverage."""
    old, new = shape(before), shape(after)
    if (not old or not new or old[:2] != new[:2] or ast.dump(old[3]) != ast.dump(new[3])
            or ast.dump(before) == ast.dump(after)):
        return None
    data = profile()
    if not data:
        return None
    site = data["sites"].get(f"{path}:{before.lineno}:{before.col_offset}:{old[1]}")
    if not site:
        return None
    opaque, witnesses = site
    mask = int(data["pool"][opaque], 16)
    operation = OPS[old[0]]
    for tagged, idx in witnesses:
        v = value_of(tagged)
        try:
            if old[1] == "r":
                differs = operation(v, old[2]) != operation(v, new[2])
            else:
                differs = operation(old[2], v) != operation(new[2], v)
        except (TypeError, ValueError, OverflowError):
            differs = True
        if differs:
            mask |= int(data["pool"][idx], 16)
    hits = set(data["incomplete"])
    if candidates is not None:
        hits.intersection_update(candidates)
    while mask:
        bit = mask & -mask
        hits.add(data["tests"][bit.bit_length() - 1])
        mask ^= bit
    return hits
