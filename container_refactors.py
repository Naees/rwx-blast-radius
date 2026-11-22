"""Recognize equivalent operations on local containers of a proven built-in type."""
import ast


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


class Normalize(ast.NodeTransformer):
    def __init__(self, tree):
        nodes = list(ast.walk(tree))
        self.builtins = BUILTINS - {name for node in nodes for name in bindings(node)}
        if any((isinstance(n, ast.alias) and n.name == "*")
               or (isinstance(n, ast.Name) and n.id in REFLECTION)
               or (isinstance(n, ast.Attribute) and n.attr in REFLECTION) for n in nodes):
            self.builtins.clear()
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

    def visit_If(self, node):
        test = node.test
        name = None
        negate = False
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            negate, test = True, test.operand
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
            node.test = ast.UnaryOp(op=ast.Not(), operand=name) if negate else name
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
    new = patch.apply(source)
    trees = [ast.parse(source), ast.parse(new)]
    # ast.parse accepts constructs that the compiler rejects, including jumps
    # outside a loop and invalid declarations.
    compile(trees[1], "<changed source>", "exec")
    for tree in trees:
        Normalize(tree).visit(tree)
    return ast.dump(trees[0]) == ast.dump(trees[1])
