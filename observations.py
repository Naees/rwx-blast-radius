"""Query operand relationships recorded while the unmodified baseline ran."""

import ast
from pathlib import Path

from mapdata import load as load_map


def _truth(op, state):
    if state == "error":
        return None
    same, rel = state
    if op == "Is":
        return same == "1"
    if op == "IsNot":
        return same == "0"
    if rel == "?":
        return None
    if op == "Eq":
        return rel == "="
    if op == "NotEq":
        return rel != "="
    if rel == "!":
        return None
    return {"Lt": rel == "<", "LtE": rel in "<=", "Gt": rel == ">",
            "GtE": rel in ">="}.get(op)


class Observations:
    def __init__(self, path=Path(__file__).parent / "maps/observations.json.gz"):
        data = load_map(path)
        self.sites = data["sites"]
        self.pool = [int(mask, 16) for mask in data["pool"]]
        self.names = data["tests"]
        self.incomplete = frozenset(data["incomplete"])

    def affected(self, path, before, after):
        if type(before) != type(after):
            return None
        if isinstance(before, ast.Compare):
            if len(before.ops) != 1 or len(after.ops) != 1:
                return None
            old_op, new_op = type(before.ops[0]).__name__, type(after.ops[0]).__name__
            identity_equality = {"Is", "IsNot", "Eq", "NotEq"}
            order = {"Lt", "LtE", "Gt", "GtE"}
            if not ({old_op, new_op} <= identity_equality or {old_op, new_op} <= order):
                return None
            old_args, new_args = [before.left, *before.comparators], [after.left, *after.comparators]
        elif isinstance(before, ast.BoolOp):
            old_op, new_op = type(before.op).__name__, type(after.op).__name__
            old_args, new_args = before.values, after.values
            if old_op == new_op:
                return None
            if any(isinstance(n, (ast.Call, ast.NamedExpr, ast.Await, ast.Yield))
                   for arg in old_args[1:] for n in ast.walk(arg)):
                # A different short-circuit point could change side effects.
                return None
        else:
            return None
        if [ast.dump(x) for x in old_args] != [ast.dump(x) for x in new_args]:
            return None
        key = f"{path}:{before.lineno}:{before.col_offset}:{old_op}"
        if key not in self.sites:
            return None

        mask = 0
        for state, index in self.sites[key].items():
            if old_op in ("And", "Or"):
                # An AND that reached true saw only truthy operands. Likewise,
                # an OR that reached false saw only falsey operands.
                unchanged = (old_op, state) in {("And", "T"), ("Or", "F")}
            else:
                # Identity with the literal None determines both operands when
                # they are identical, even for a partially classified trace.
                if state == "1?" and any(isinstance(arg, ast.Constant) and arg.value is None for arg in old_args):
                    state = "1="
                left, right = _truth(old_op, state), _truth(new_op, state)
                unchanged = left is not None and left == right
            if not unchanged:
                mask |= self.pool[index]
        selected = set()
        while mask:
            bit = mask & -mask
            selected.add(self.names[bit.bit_length() - 1])
            mask ^= bit
        return frozenset(selected)
