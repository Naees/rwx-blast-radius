"""Compare integer index edits against the sequence shapes seen on the baseline."""
import ast
from functools import lru_cache
from pathlib import Path

from impact import _changes, source_window
from mapdata import load


def index(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        val = node.operand.value
        return -val if type(val) is int and 0 <= val < 2**31 else None
    if isinstance(node, ast.Constant) and type(node.value) is int and abs(node.value) < 2**31:
        return node.value
    return None


def access(node):
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return 'item', node.value, node.slice
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'seq_get'
            and len(node.args) == 2 and not node.keywords and not isinstance(node.args[0], ast.Starred)):
        return 'optional', node.args[0], node.args[1]
    return None


def key(node, kind):
    return f"{node.lineno}:{node.col_offset}:{kind}"


def equivalent(shape, left, right, optional):
    length, labels = shape
    def token(pos):
        if pos < 0:
            pos += length
        if pos < 0 or pos >= length:
            return -2 if optional else -1
        return labels[pos] if labels else pos + 1
    old, new = token(left), token(right)
    # A caught IndexError and an in-bounds None travel through different
    # exception paths. Keep such edits even though their return values agree.
    return old == new and (old >= 0 or optional and old == -2)


@lru_cache(1)
def profile():
    path = Path(__file__).parent / "maps/sequence_index.json.gz"
    if not path.exists():
        return {}
    data = load(path)
    files = {}
    for full, entry in data["sites"].items():
        path, rest = full.split(":", 1)
        files.setdefault(path, {})[rest] = entry
    return files, data["tests"], set(data["incomplete"]), [int(mask, 16) for mask in data["pool"]]


def select(patch, source, candidates, data=None, *, source_readers=()):
    if patch.old_path != patch.new_path or not patch.removed_lines:
        return None
    rows = patch.removed_lines
    if not any(char.isdigit() for h in patch.hunks for text in h.body if text.startswith("-") for char in text):
        return None
    data = profile() if data is None else data
    if not data:
        return None
    files, tests, incomplete, masks = data
    sites = files.get(patch.path)
    if not sites or not any(int(loc.partition(":")[0]) in rows for loc in sites):
        return None
    a, b = source_window(patch, source, patch.apply(source))
    old, new = ast.parse(a), ast.parse(b)
    linked = None
    for trail in _changes(old, new, []):
        pair = next(((n, m) for n, m, _ in reversed(trail) if access(n) and access(m)), None)
        if pair is None or (linked is not None and pair[0] is not linked[0]):
            return None
        linked = pair
    if linked is None:
        return None
    x, y = linked
    kind, container, prior = access(x)
    other, updated, after = access(y)
    i, j = index(prior), index(after)
    if kind != other or i is None or j is None or i == j or ast.dump(container) != ast.dump(updated):
        return None
    entry = sites.get(key(x, kind))
    if entry is None:
        return None
    unknown, observations = entry
    seen, diff = 0, masks[unknown]
    for shape, mask in observations:
        bits = masks[mask]
        seen |= bits
        if not equivalent(shape, i, j, kind == "optional"):
            diff |= bits
    removable = seen & ~diff
    selected = set(candidates)
    while removable:
        bit = removable & -removable
        selected.discard(tests[bit.bit_length()-1])
        removable ^= bit
    return selected | ((incomplete | set(source_readers)) & candidates)
