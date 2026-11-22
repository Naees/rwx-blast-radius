"""Recognize equivalent operations on local containers of a proven built-in type."""
import ast
import types
from functools import lru_cache
from impact import source_window, _equal


SEQUENCES = (ast.List, ast.ListComp, ast.Tuple, ast.Dict, ast.DictComp, ast.Set, ast.SetComp)
LISTS = (ast.List, ast.ListComp)
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
BUILTINS = {"len", "bool", "reversed"}
REFLECTION = {"eval", "exec", "globals", "locals", "vars", "__builtins__", "_getframe"}


def bindings(node):
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        yield node.id
    elif isinstance(node, ast.arg):
        yield node.arg
    elif isinstance(node, ast.alias):
        yield node.asname or node.name.split(".")[0]
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        yield from node.names
    elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        yield node.name
    elif isinstance(node, ast.MatchMapping):
        yield node.rest


def literal_stores(target, value):
    if isinstance(target, ast.Name) and isinstance(value, SEQUENCES):
        yield target, "list" if isinstance(value, LISTS) else "container"
    elif (isinstance(target, (ast.List, ast.Tuple)) and isinstance(value, (ast.List, ast.Tuple))
          and len(target.elts) == len(value.elts) and not any(isinstance(x, ast.Starred) for x in value.elts)):
        for lhs, rhs in zip(target.elts, value.elts):
            yield from literal_stores(lhs, rhs)


def local_types(function):
    # Reads in a nested scope may close over this local. Any nested binding of
    # its name is treated as unknown, including nonlocal writes.
    nodes = list(ast.walk(function))
    approved, known, preserved_lists = {}, {}, []
    work = list(function.body)
    while work:
        node = work.pop()
        if isinstance(node, SCOPES):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add):
                    for left, right in ((node.value.left, node.value.right), (node.value.right, node.value.left)):
                        if isinstance(left, LISTS) and isinstance(right, ast.Name) and right.id == target.id:
                            preserved_lists.append(target)
                for name, kind in literal_stores(target, node.value):
                    approved[name] = kind
                    if known.get(name.id, kind) != kind:
                        kind = "container"
                    known[name.id] = kind
        work.extend(ast.iter_child_nodes(node))
    approved.update({name: "list" for name in preserved_lists if known.get(name.id) == "list"})
    for node in nodes:
        if node not in approved:
            for name in bindings(node):
                known.pop(name, None)
    return known


def argument(call, name):
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name
            and len(call.args) == 1 and isinstance(call.args[0], ast.Name) and not call.keywords):
        return call.args[0]


def builtin_names(tree):
    nodes = ast.walk(tree)
    allowed = set(BUILTINS)
    for n in nodes:
        if ((isinstance(n, ast.alias) and n.name == "*")
                or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in REFLECTION)
                or (isinstance(n, ast.Attribute) and n.attr in REFLECTION)):
            return set()
        allowed.difference_update(bindings(n))
    # A local with a name like 'vars' is ordinary data. co_names distinguishes
    # global or attribute references from fast locals and closure variables.
    codes = [compile(tree, "<bindings>", "exec")]
    while codes:
        code = codes.pop()
        if REFLECTION.intersection(code.co_names):
            return set()
        codes.extend(value for value in code.co_consts if isinstance(value, types.CodeType))
    return allowed


@lru_cache(maxsize=8)
def file_builtins(source):
    return builtin_names(ast.parse(source))


class Normalize(ast.NodeTransformer):
    def __init__(self, allowed):
        self.builtins = allowed
        self.types = {}

    def visit_FunctionDef(self, node):
        previous, self.types = self.types, local_types(node)
        node.body = [self.visit(stmt) for stmt in node.body]
        self.types = previous
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        previous, self.types = self.types, {}
        node = self.generic_visit(node)
        self.types = previous
        return node

    def condition(self, test):
        # Every operand of an and/or used directly as a branch condition
        # is consumed only for its truth, including the final operand.
        if isinstance(test, ast.BoolOp):
            test.values = [self.condition(child) for child in test.values]
            return test
        original = test
        name = None
        negate = False
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            negate, test = True, test.operand
            if isinstance(test, ast.BoolOp):
                original.operand = self.condition(test)
                return original
        if "bool" in self.builtins:
            name = argument(test, "bool")
        if "len" in self.builtins:
            name = name or argument(test, "len")
            if (isinstance(test, ast.Compare) and len(test.ops) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and type(test.comparators[0].value) is int and test.comparators[0].value == 0
                    and isinstance(test.ops[0], (ast.Eq, ast.NotEq, ast.Gt))):
                name = argument(test.left, "len")
                negate ^= isinstance(test.ops[0], ast.Eq)
        if name and name.id in self.types:
            return ast.UnaryOp(op=ast.Not(), operand=name) if negate else name
        return original

    def visit_If(self, node):
        node.test = self.condition(node.test)
        return self.generic_visit(node)

    visit_While = visit_If

    def visit_Assign(self, node):
        if "reversed" not in self.builtins or len(node.targets) != 1:
            return node
        name, target = argument(node.value, "reversed"), node.targets[0]
        if (name and self.types.get(name.id) == "list" and isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name) and target.value.id == name.id
                and isinstance(target.slice, ast.Slice)
                and target.slice.lower is target.slice.upper is target.slice.step is None):
            return ast.Expr(value=ast.Call(func=ast.Attribute(value=name, attr="reverse", ctx=ast.Load()),
                                           args=[], keywords=[]))
        return node


def equivalent(patch, source):
    if patch.old_path != patch.new_path:
        return False
    lines = [line[1:] for h in patch.hunks for line in h.body if line[:1] in ("+", "-")]
    reverse = any(".reverse(" in line for line in lines) and any("reversed(" in line for line in lines)
    truth = (any(line.lstrip().startswith(("if ", "while ")) for line in lines)
             and any("len(" in line or "bool(" in line for line in lines))
    if not (reverse or truth):
        return False
    old, new = source_window(patch, source, patch.apply(source))
    trees = [ast.parse(old), ast.parse(new)]
    try:
        compile(trees[1], "<changed source>", "exec")
    except (SyntaxError, ValueError):
        return False
    available = file_builtins(source) if old != source else builtin_names(trees[0])
    available = available & builtin_names(trees[1])
    if not available:
        return False
    for tree in trees:
        Normalize(available).visit(tree)
    return _equal(trees[0], trees[1])
