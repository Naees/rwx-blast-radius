"""Bound existing literal default edits to measured calls of a pinned function."""

import ast
import gzip
import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path

from mapdata import load

MAP = Path(__file__).with_name("maps") / "default_args.json.gz"
SCALARS = (bool, int, float, str, bytes, type(None))


def scalar(node):
    if isinstance(node, ast.Constant) and type(node.value) in SCALARS:
        return type(node.value)
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) in (int, float)
    ):
        return type(node.operand.value)
    return None


class References(ast.NodeVisitor):
    """Names that escape direct calls and might be inspected by other code."""

    def __init__(self):
        self.escaped = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.escaped.add(node.id)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.escaped.add(node.attr)
        self.visit(node.value)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.asname and alias.asname != alias.name:
                self.escaped.add(alias.name)

    def visit_Call(self, node):
        # A named or bound callable used directly as the callee executes its
        # body. Passing it as an argument, aliasing it or storing it escapes.
        if isinstance(node.func, ast.Attribute):
            self.visit(node.func.value)
        elif not isinstance(node.func, ast.Name):
            self.visit(node.func)
        if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                self.escaped.add(node.args[1].value)
        for value in [*node.args, *node.keywords]:
            self.visit(value)


def _reads_defaults(tree):
    # A dynamically retrieved callable could expose defaults without being
    # invoked. The pinned optimizer and executor only inspect parameter names.
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    for node, parent in parents.items():
        if isinstance(node, ast.ImportFrom) and node.module == "inspect":
            if any(
                alias.name in {"signature", "getfullargspec", "getcallargs", "*"}
                for alias in node.names
            ):
                return True
        label = getattr(node, "attr", None) or getattr(node, "id", None)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            label = node.value
        if label in {"__defaults__", "__kwdefaults__", "getcallargs", "apply_defaults"}:
            return True
        if label not in {"signature", "getfullargspec"}:
            continue
        if not isinstance(parent, ast.Call) or parent.func is not node:
            return True
        access = parents.get(parent)
        if not isinstance(access, ast.Attribute):
            return True
        if label == "getfullargspec" and access.attr in {
            "args",
            "kwonlyargs",
            "varargs",
            "varkw",
        }:
            continue
        lookup = parents.get(access)
        if (
            label == "signature"
            and access.attr == "parameters"
            and isinstance(lookup, ast.Call)
        ):
            if isinstance(lookup.func, ast.Name) and lookup.func.id in {
                "enumerate",
                "set",
                "list",
                "tuple",
            }:
                continue
        return True
    return False


def _functions(tree):
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and not node.decorator_list
            and not node.keywords
        ):
            yield from _functions(node)
        elif (
            isinstance(node, ast.FunctionDef)
            and not node.decorator_list
            and not node.name.startswith("__")
            and not any(
                isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)
            )
        ):
            yield node


def build(base):
    base = Path(base)
    references = References()
    files = {}
    reflective_terms = (
        b"signature",
        b"getfullargspec",
        b"__defaults__",
        b"__kwdefaults__",
        b"getcallargs",
        b"apply_defaults",
    )
    for directory in ("sqlglot", "tests"):
        for path in sorted((base / directory).rglob("*.py")):
            raw = path.read_bytes()
            tree = ast.parse(raw)
            references.visit(tree)
            if any(term in raw for term in reflective_terms) and _reads_defaults(tree):
                return {}
            if directory == "tests":
                continue
            sites = []
            for fn in _functions(tree):
                body = fn.body
                if ast.get_docstring(fn, clean=False) is not None:
                    body = body[1:]
                if not body:
                    continue
                for default in [*fn.args.defaults, *fn.args.kw_defaults]:
                    if (
                        default is None
                        or not scalar(default)
                        or default.lineno != default.end_lineno
                    ):
                        continue
                    sites.append(
                        [
                            fn.name,
                            default.lineno,
                            default.col_offset,
                            default.end_col_offset,
                            body[0].lineno,
                            body[-1].end_lineno,
                        ]
                    )
            if sites:
                files[path.relative_to(base).as_posix()] = [
                    hashlib.sha256(raw).hexdigest(),
                    sites,
                ]
    output = {}
    for path, (digest, entries) in files.items():
        direct = [site[1:] for site in entries if site[0] not in references.escaped]
        if direct:
            output[path] = [digest, direct]
    return output


@lru_cache(1)
def profile(base):
    return load(MAP) if MAP.exists() else build(base)


def select(patch, source, base, deps):
    if (
        patch.binary
        or patch.old_path != patch.new_path
        or not patch.path.startswith("sqlglot/")
    ):
        return None
    removed = patch.removed_lines
    if not removed or removed != patch.added_lines or len(removed) != 1:
        return None
    number = next(iter(removed))
    line = source.splitlines()[number - 1].encode()
    if b"=" not in line:
        return None
    entry = profile(base).get(patch.path)
    if not entry or hashlib.sha256(source.encode()).hexdigest() != entry[0]:
        return None
    new = patch.apply(source).splitlines()[number - 1].encode()
    for at, begin, end, start_body, end_body in entry[1]:
        if at != number:
            continue
        prefix, suffix = line[:begin], line[end:]
        if (
            not new.startswith(prefix)
            or not new.endswith(suffix)
            or len(new) < len(prefix) + len(suffix)
        ):
            continue
        replacement = new[begin : len(new) - len(suffix)]
        try:
            old_value = ast.parse(line[begin:end], mode="eval").body
            new_value = ast.parse(replacement, mode="eval").body
        except (SyntaxError, ValueError):
            continue
        if (
            not scalar(old_value)
            or scalar(new_value) is not scalar(old_value)
            or new_value.col_offset != 0
            or new_value.end_col_offset != len(replacement)
        ):
            continue
        body = range(start_body, end_body + 1)
        if deps.import_initializers(patch.path, body):
            return None
        calls = set().union(*(deps.line(patch.path, n) for n in body))
        if calls:
            return calls | set(deps.incomplete)
    return None


if __name__ == "__main__":
    snapshot = build(sys.argv[1])
    if not snapshot:
        raise SystemExit("Default reflection requires broad selection")
    path = Path(sys.argv[2])
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(snapshot, separators=(",", ":")).encode())
    print(
        f"{sum(len(rows) for _, rows in snapshot.values())} eligible literal defaults in {len(snapshot)} files"
    )
