"""Index direct and qualified reads of pinned class and module scalar constants."""
import ast
import gzip
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from configmap import literal, members, module_members, module_name

PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


class Reads(ast.NodeVisitor):
    def __init__(self, path, names, bare, qualified):
        self.path, self.names = path, names
        self.bare, self.qualified = bare, qualified
        self.function = None
        self.default = None

    def visit_FunctionDef(self, node):
        parent, default = self.function, self.default
        for item in node.decorator_list:
            self.visit(item)
        # Evaluated when the function is defined. All subsequent calls can see
        # the resulting captured value, including calls from other tests.
        self.default = (node.lineno, node.end_lineno)
        self.visit(node.args)
        self.default, self.function = None, node
        for stmt in node.body:
            self.visit(stmt)
        self.default, self.function = default, parent

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        old, self.function = self.function, None
        self.generic_visit(node)
        self.function = old

    def record(self, attr, node, target):
        if attr not in self.names:
            return
        if self.default:
            target[attr].add((self.path, *self.default))
        elif self.function:
            target[attr].add((self.path, node.lineno, node.end_lineno))
        else:
            target[attr].add(None)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.record(node.id, node, self.bare)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.asname and alias.name in self.names:
                self.bare[alias.name].add(None)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.record(node.attr, node, self.qualified)
        self.generic_visit(node)

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
            self.record(node.args[1].value, node, self.qualified)
        self.generic_visit(node)


def main():
    root, output = [Path(a).resolve() for a in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Expected pinned base")
    if subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Dirty base checkout")
    paths = [*sorted((root / "sqlglot").rglob("*.py")), *sorted((root / "tests").rglob("*.py"))]
    symbols = set()
    for path in paths:
        if path.is_relative_to(root / "tests"):
            continue
        module = module_name(str(path.relative_to(root)))
        tree = ast.parse(path.read_bytes())
        for symbol, stmt in [*members(tree, module), *module_members(tree, module)]:
            if symbol.split(".")[-1].isupper() and type(literal(stmt.value)) in (int, float, bool, str, bytes):
                symbols.add(symbol)
    names = {sym.split(".")[-1] for sym in symbols}
    bare, qualified = defaultdict(set), defaultdict(set)
    for path in paths:
        Reads(str(path.relative_to(root)), names, bare, qualified).visit(ast.parse(path.read_bytes()))

    def encode(data):
        return {k: [None] if None in v else sorted(v) for k, v in data.items()}
    with open(output, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps({"symbols": sorted(symbols), "bare": encode(bare),
                                 "qualified": encode(qualified)}, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()
