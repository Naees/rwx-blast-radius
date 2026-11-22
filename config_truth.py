"""Check whether a changed Boolean class flag can affect its observed predicates."""

import ast
from functools import lru_cache
from pathlib import Path

import bool_values
from config_sites import context
from configmap import module_name
from mapdata import load


@lru_cache(maxsize=1)
def profile():
    path = Path(__file__).parent / "maps/config_truth.json.gz"
    if not path.is_file():
        return None
    data, conditions = load(path), bool_values.profile()
    if not conditions or data["tests"] != conditions["tests"]:
        return None
    return data, conditions, tuple(int(mask, 16) for mask in data["pool"])


def flipped(patch):
    if patch.old_path != patch.new_path or len(patch.removed_lines) != 1 or len(patch.added_lines) != 1:
        return None
    values = {}
    for hunk in patch.hunks:
        for line in hunk.body:
            if line[0] not in "+-":
                continue
            try:
                statements = ast.parse(line[1:].strip()).body
            except (ValueError, SyntaxError):
                return None
            if len(statements) != 1 or not isinstance(statements[0], (ast.Assign, ast.AnnAssign)):
                return None
            node = statements[0].value
            if not isinstance(node, ast.Constant) or type(node.value) is not bool:
                return None
            values[line[0]] = node.value
    if len(values) == 2 and values["+"] is not values["-"]:
        return values["-"]
    return None


def predicate(conditions, guard):
    key, tree, flag = guard
    site = conditions["sites"].get(key)
    if site is None:
        return 0, 0
    opaque, vectors = site
    uncertain = seen = int(conditions["pool"][opaque], 16)
    for vector, index in vectors.items():
        bits = int(conditions["pool"][index], 16)
        seen |= bits
        prefix, suffix = vector[:flag], vector[flag + 1:]
        a = bool_values.result(tree, prefix + "T" + suffix)
        b = bool_values.result(tree, prefix + "F" + suffix)
        if a is None or b is None or a != b:
            uncertain |= bits
    return seen, uncertain


def select(patch, impact, source, deps, candidates):
    if not impact.global_change or len(impact.changed_names) != 1:
        return None
    prior = flipped(patch)
    if prior is None:
        return None
    tables = profile()
    if not tables:
        return None
    data, conditions, pool = tables
    name, = impact.changed_names
    symbol = ".".join((module_name(patch.path), *impact.classes, name))
    record = data["symbols"].get(symbol)
    if record is None or record[0] is not prior or context(patch, impact, source, deps) is None:
        return None
    _, opaque, rows = record
    keep = seen = pool[opaque]
    for _, guard, index in rows:
        reads = pool[index]
        seen |= reads
        if guard is None:
            keep |= reads
        else:
            observed, uncertain = predicate(conditions, guard)
            keep |= reads & (uncertain | ~observed)
    # An independent read profile or source reader may know about another
    # dependency. Only remove tests seen reading this exact flag and proven
    # insensitive at every observed read location.
    remove = seen & ~keep
    selected = set(candidates)
    protected = set(data["incomplete"]) | set(conditions["incomplete"]) | set(deps.source_readers(patch.path))
    while remove:
        bit = remove & -remove
        test = data["tests"][bit.bit_length() - 1]
        if test not in protected:
            selected.discard(test)
        remove ^= bit
    return selected
