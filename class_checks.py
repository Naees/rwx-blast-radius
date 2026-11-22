"""Select tests whose observed runtime types can distinguish two isinstance targets."""

import ast
import re
from functools import lru_cache
from pathlib import Path

from impact import _changes, source_window
from mapdata import load

BUILTINS = frozenset(("object", "type", "bool", "int", "float", "complex", "str", "bytes", "bytearray",
                      "list", "tuple", "dict", "set", "frozenset", "range"))
TARGET = re.compile(r"\b(?:exp\s*\.|" + "|".join(sorted(BUILTINS)) + r")\b")


def tags(node, depth=0):
    if isinstance(node, ast.Tuple) and depth < 3 and len(node.elts) <= 12:
        parts = [tags(n, depth + 1) for n in node.elts]
        if all(part is not None for part in parts):
            return tuple(label for part in parts for label in part)
    elif isinstance(node, ast.Name) and node.id in BUILTINS:
        return (node.id,)
    elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
          and node.value.id == "exp" and not node.attr.startswith("_")):
        return ("exp." + node.attr,)
    return None


def target(call):
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "isinstance"
            and not call.keywords and len(call.args) == 2 and not isinstance(call.args[0], ast.Starred)):
        return tags(call.args[1])
    return None


def location(node):
    return f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"


@lru_cache(1)
def profile():
    path = Path(__file__).parent / "maps/class_checks.json.gz"
    if not path.exists():
        return None
    data = load(path)
    data["pool"] = [int(mask, 16) for mask in data["pool"]]
    data["lines"] = {
        path: {line for loc in sites for start, _, end, _ in [loc.split(":")]
               for line in range(int(start), int(end) + 1)}
        for path, sites in data["sites"].items()
    }
    return data


def edited_check(patch, source):
    old, new = source_window(patch, source, patch.apply(source))
    before, after = ast.parse(old), ast.parse(new)
    candidate = None
    for trail in _changes(before, after, []):
        pair = next(((a, b) for a, b, _ in reversed(trail) if isinstance(a, ast.Call)), None)
        if pair is None or (candidate and candidate[0] is not pair[0]):
            return None
        candidate = pair
    if not candidate:
        return None
    old, new = candidate
    a, b = target(old), target(new)
    if a is None or b is None or ast.dump(old.args[0]) != ast.dump(new.args[0]):
        return None
    return location(old), a, b


def select(patch, source, candidates):
    """Return a subset of candidates, or None if the complete edit is unsupported."""
    if patch.old_path != patch.new_path or not any(
        TARGET.search(line[1:]) for h in patch.hunks for line in h.body if line[0] in "+-"
    ):
        return None
    data = profile()
    sites = data and data["sites"].get(patch.path)
    if not sites:
        return None
    touched = patch.removed_lines | patch.insertions
    if data["lines"][patch.path].isdisjoint(touched):
        return None
    pair = edited_check(patch, source)
    if pair is None or pair[0] not in sites:
        return None
    key, before, after = pair
    opaque, observations = sites[key]
    mask = data["pool"][opaque]
    matches = {}
    for ctx, mro_index, tests in observations:
        if ctx not in matches:
            bindings = data["contexts"][ctx]
            if any(name not in bindings for name in (*before, *after)):
                matches[ctx] = None
            else:
                matches[ctx] = ({bindings[n] for n in before}, {bindings[n] for n in after})
        targets = matches[ctx]
        mro = data["mros"][mro_index]
        if targets is None or targets[0].isdisjoint(mro) != targets[1].isdisjoint(mro):
            mask |= data["pool"][tests]
    if not any(targets is not None for targets in matches.values()):
        return None
    result = set(data["incomplete"]) & candidates
    while mask:
        bit = mask & -mask
        name = data["tests"][bit.bit_length() - 1]
        if name in candidates:
            result.add(name)
        mask ^= bit
    return result
