"""Check whether a modified assignment preserves an observed Python value at every execution."""

import ast
from functools import lru_cache
from pathlib import Path
from impact import source_window
from mapdata import load


@lru_cache(1)
def sites():
    path = Path(__file__).parent / "maps/optimizer_witness.json.gz"
    return load(path) if path.is_file() else {}


@lru_cache(1)
def locations():
    result = {}
    for site in sites():
        path, line, _ = site.rsplit(":", 2)
        result.setdefault(path, set()).add(int(line))
    return result


def _tree(node):
    return ast.dump(node, include_attributes=False)


def changed_assignment(patch, source):
    old, new = source_window(patch, source, patch.apply(source))
    a, b = ast.parse(old), ast.parse(new)
    before = [n for n in ast.walk(a) if isinstance(n, ast.Assign)]
    after = [n for n in ast.walk(b) if isinstance(n, ast.Assign)]
    if len(before) != len(after):
        return None
    differing = [
        (x, y) for x, y in zip(before, after) if _tree(x.value) != _tree(y.value)
    ]
    if len(differing) != 1:
        return None
    x, y = differing[0]
    prior, changed = x.value, y.value
    if any(
        isinstance(
            n,
            (
                ast.NamedExpr,
                ast.Lambda,
                ast.comprehension,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
            ),
        )
        for n in ast.walk(changed)
    ):
        return None
    if any(
        isinstance(n, ast.Name) and n.id in {"__class__", "super"}
        for n in ast.walk(changed)
    ):
        return None
    y.value = prior
    if _tree(a) != _tree(b):
        return None
    return prior, changed


def parameters(call, signature):
    if any(isinstance(a, ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    ):
        return None
    _, _, positional, pos_only, kw_only, defaults, kw_defaults, extras = signature
    if len(call.args) > len(positional):
        return None
    result = list(positional[: len(call.args)])
    eligible = set(positional[pos_only:]) | set(kw_only)
    for k in call.keywords:
        if (
            k.arg in result
            or k.arg in positional[:pos_only]
            or (k.arg not in eligible and not extras)
        ):
            return None
        result.append(k.arg)
    required = set(positional[: len(positional) - defaults]) | (
        set(kw_only) - set(kw_defaults)
    )
    return result if required.issubset(result) else None


def equivalent_call(old, new, signature):
    if not isinstance(old.func, ast.Name) or _tree(old.func) != _tree(new.func):
        return False

    def values(call):
        return [_tree(x) for x in [*call.args, *(k.value for k in call.keywords)]]

    if values(old) != values(new):
        return False
    before = parameters(old, signature)
    return before is not None and before == parameters(new, signature)


def reference(node, name):
    if isinstance(node, ast.Name) and node.id == name:
        return ""
    if (
        isinstance(node, ast.Attribute)
        and not node.attr.startswith("_")
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    ):
        return node.attr
    return None


def condition(node, name, paths):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        val = condition(node.operand, name, paths)
        return not val if val is not None else None
    if (
        not isinstance(node, ast.Compare)
        or len(node.ops) != 1
        or len(node.comparators) != 1
    ):
        return None
    left, right, op = node.left, node.comparators[0], node.ops[0]
    if not isinstance(right, ast.Constant):
        return None
    key = None
    if right.value is None and isinstance(op, (ast.Is, ast.IsNot)):
        key = reference(left, name)
        values = {v if isinstance(op, ast.Is) else not v for _, v in paths.get(key, ())}
    elif (
        type(right.value) is str
        and isinstance(op, (ast.Eq, ast.NotEq))
        and isinstance(left, ast.Attribute)
        and left.attr == "__name__"
        and isinstance(left.value, ast.Call)
        and isinstance(left.value.func, ast.Name)
        and left.value.func.id == "type"
        and len(left.value.args) == 1
        and not left.value.keywords
    ):
        key = reference(left.value.args[0], name)
        labels = [label for label, _ in paths.get(key, ())]
        if None in labels:
            return None
        values = {
            (label == right.value) if isinstance(op, ast.Eq) else (label != right.value)
            for label in labels
        }
    else:
        return None
    return next(iter(values)) if key is not None and len(values) == 1 else None


def refine(patch, impact, candidates, base):
    if (
        patch.old_path != patch.new_path
        or not patch.path.startswith("sqlglot/optimizer/")
        or impact.global_change
        or impact.invalid
        or impact.equivalent
    ):
        return set()
    if patch.removed_lines.isdisjoint(locations().get(patch.path, ())):
        return set()
    change = changed_assignment(patch, (Path(base) / patch.path).read_text())
    if change is None:
        return set()
    old, new = change
    site = f"{patch.path}:{old.lineno}:{old.col_offset}"
    unaffected = set()
    for test, rec in sites().get(site, {}).items():
        if test not in candidates:
            continue
        if isinstance(old, ast.Call) and isinstance(new, ast.Call) and "shape" in rec:
            ok = equivalent_call(old, new, rec["shape"])
        elif (
            isinstance(old, ast.Name) and isinstance(new, ast.IfExp) and "paths" in rec
        ):
            truth = condition(new.test, old.id, rec["paths"])
            ok = truth is not None and _tree(
                new.body if truth else new.orelse
            ) == _tree(old)
        else:
            ok = False
        if ok:
            unaffected.add(test)
    return unaffected
