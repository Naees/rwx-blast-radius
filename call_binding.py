"""Bound direct positional-to-keyword rewrites by tests that can inspect or interpose."""

import ast
import hashlib
from functools import lru_cache
from pathlib import Path

from impact import source_window
from mapdata import load
from optimizer_witness import equivalent_call

MAP = Path(__file__).parent / "maps/call_binding.json.gz"


@lru_cache(1)
def index():
    return load(MAP)


def same_tree(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(
        right, include_attributes=False
    )


def select(patch, source, deps, hits):
    if (
        patch.old_path != patch.new_path
        or patch.binary
        or len(patch.removed_lines) != 1
        or patch.removed_lines != patch.added_lines
    ):
        return None
    # Each introduced keyword adds an equals sign, keeping unrelated edits
    # away from the site map and AST parser. Missed rewrites use normal selection.
    delta = sum(
        line.count("=") * (1 if line[0] == "+" else -1)
        for hunk in patch.hunks
        for line in hunk.body
        if line[0] in "+-"
    )
    if delta <= 0:
        return None
    row = index()["files"].get(patch.path)
    if row is None:
        return None
    (line,) = patch.removed_lines
    locations = {k: v for k, v in row[1].items() if k.split(":")[0] == str(line)}
    if not locations or hashlib.sha256(source.encode()).hexdigest() != row[0]:
        return None
    if deps.import_initializers(patch.path, {line}):
        return None
    prior, after = source_window(patch, source, patch.apply(source))
    old, new = ast.parse(prior), ast.parse(after)
    old_calls = {
        (n.lineno, n.col_offset): n for n in ast.walk(old) if isinstance(n, ast.Call)
    }
    new_calls = {
        (n.lineno, n.col_offset): n for n in ast.walk(new) if isinstance(n, ast.Call)
    }
    for loc, (name, origin, export, shape) in locations.items():
        pos = tuple(map(int, loc.split(":")))
        a, b = old_calls.get(pos), new_calls.get(pos)
        if not (
            a
            and b
            and isinstance(a.func, ast.Name)
            and a.func.id == name
            and len(a.args) > len(b.args)
            and equivalent_call(a, b, shape)
        ):
            continue
        args, kw = b.args, b.keywords
        b.args, b.keywords = a.args, a.keywords
        ok = same_tree(old, new)
        b.args, b.keywords = args, kw
        if ok:
            uncertain = set(index()["uncertain"])
            return (
                (set(hits) & uncertain)
                | set(index()["outside"])
                | set(deps.source_readers(patch.path))
                | (set(hits) - deps.tests)
            )
    return None
