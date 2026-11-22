"""Bound existing literal default edits to measured calls of a pinned function."""

import ast
import gzip
import hashlib
import json
import marshal
import sys
from functools import lru_cache
from importlib.machinery import PathFinder
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
        self.members = set()
        self.class_body = False
        self.shadowed_exception = False

    def visit_Name(self, node):
        if node.id == "NotImplementedError" and not isinstance(node.ctx, ast.Load):
            self.shadowed_exception = True
        if isinstance(node.ctx, ast.Load):
            self.escaped.add(node.id)
            if self.class_body:
                self.members.add(node.id)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.escaped.add(node.attr)
            self.members.add(node.attr)
        self.visit(node.value)

    def visit_ClassDef(self, node):
        previous, self.class_body = self.class_body, True
        self.generic_visit(node)
        self.class_body = previous

    def visit_FunctionDef(self, node):
        # A default, annotation or decorator in a class body can refer to
        # a previously defined method by its bare name. Inside the method,
        # a local with that spelling cannot designate the class member
        # without an attribute load (or an earlier class-body alias).
        for child in [*node.decorator_list, node.args, node.returns]:
            if child is not None:
                self.visit(child)
        previous, self.class_body = self.class_body, False
        for statement in node.body:
            self.visit(statement)
        self.class_body = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if (alias.asname or alias.name) == "NotImplementedError":
                self.shadowed_exception = True
            if alias.asname and alias.asname != alias.name:
                self.escaped.add(alias.name)

    def visit_Import(self, node):
        if any(
            (alias.asname or alias.name) == "NotImplementedError"
            for alias in node.names
        ):
            self.shadowed_exception = True

    @staticmethod
    def namespace(node):
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "__dict__"
            or isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "vars"
        )

    def visit_Subscript(self, node):
        if self.namespace(node.value) and isinstance(node.slice, ast.Constant):
            self.members.add(node.slice.value)
        self.generic_visit(node)

    def visit_Call(self, node):
        # A named or bound callable used directly as the callee executes its
        # body. Passing it as an argument, aliasing it or storing it escapes.
        if isinstance(node.func, ast.Attribute):
            if (
                self.namespace(node.func.value)
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                self.members.add(node.args[0].value)
            self.visit(node.func.value)
        elif not isinstance(node.func, ast.Name):
            self.visit(node.func)
        if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                self.escaped.add(node.args[1].value)
                self.members.add(node.args[1].value)
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
        if isinstance(node, ast.keyword):
            label = node.arg
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            label = node.value
        if label in {
            "__defaults__",
            "__kwdefaults__",
            "getcallargs",
            "apply_defaults",
            "f_locals",
            "currentframe",
            "_getframe",
            "capture_locals",
        }:
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


def _bindings(tree, name):
    """Find possible module bindings, including optional-import fallbacks."""
    for stmt in tree.body:
        if isinstance(stmt, (ast.Try, ast.If)):
            for block in [
                stmt.body,
                stmt.orelse,
                getattr(stmt, "finalbody", []),
                *(handler.body for handler in getattr(stmt, "handlers", [])),
            ]:
                yield from _bindings(ast.Module(body=block, type_ignores=[]), name)
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if (alias.asname or alias.name) == name:
                    yield (stmt.module, alias.name) if not stmt.level else False
                elif alias.name == "*":
                    yield False
        elif isinstance(stmt, ast.Import):
            if any(
                (alias.asname or alias.name.split(".")[0]) == name
                for alias in stmt.names
            ):
                yield False
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name == name:
                args = stmt.args if isinstance(stmt, ast.FunctionDef) else None
                arg = [*args.posonlyargs, *args.args] if args else []
                body = (
                    stmt.body[1:]
                    if ast.get_docstring(stmt, clean=False) is not None
                    else stmt.body
                )
                yield bool(
                    args
                    and len(arg) == 1
                    and not (
                        args.vararg
                        or args.kwarg
                        or args.kwonlyargs
                        or args.defaults
                        or stmt.decorator_list
                    )
                    and len(body) == 1
                    and isinstance(body[0], ast.Return)
                    and isinstance(body[0].value, ast.Name)
                    and body[0].value.id == arg[0].arg
                )
        elif any(
            isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == name
            for n in ast.walk(stmt)
        ):
            yield False


@lru_cache(64)
def _decorator_module(base, module):
    if not module:
        return None
    path = base.joinpath(*module.split(".")).with_suffix(".py")
    if not path.is_file():
        if "." in module:
            return None
        spec = PathFinder.find_spec(module)
        if not spec or not spec.origin or not spec.origin.endswith(".py"):
            return None
        path = Path(spec.origin)
    return ast.parse(path.read_bytes())


def _identity_decorator(base, tree, name, parents=()):
    bindings = list(_bindings(tree, name))
    if not bindings:
        return False
    for item in bindings:
        if item is True:
            continue
        if not item or item in parents:
            return False
        source = _decorator_module(base, item[0])
        if source is None or not _identity_decorator(
            base, source, item[1], (*parents, item)
        ):
            return False
    return True


def _functions(tree, identities, member=False):
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and all(
                not member and isinstance(d, ast.Name) and d.id in identities
                for d in node.decorator_list
            )
            and not node.keywords
        ):
            yield from _functions(node, identities, True)
        elif (
            isinstance(node, ast.FunctionDef)
            and not node.decorator_list
            and not node.name.startswith("__")
            and not any(
                isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)
            )
        ):
            yield member, node


def _inert_body(fn, body):
    # These statements expose no parameter value, even when the function is
    # called without reaching an instrumentable body line. Exclude arbitrary
    # calls, exception constructors, closures and introspection of locals.
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, (ast.Return, ast.Expr)):
        return stmt.value is None or isinstance(stmt.value, ast.Constant)
    raise_builtin = (
        isinstance(stmt, ast.Raise)
        and stmt.cause is None
        and isinstance(stmt.exc, ast.Name)
        and stmt.exc.id == "NotImplementedError"
        and not any(
            arg.arg == "NotImplementedError"
            for arg in ast.walk(fn.args)
            if isinstance(arg, ast.arg)
        )
    )
    return "NotImplementedError" if raise_builtin else False


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
        b"f_locals",
        b"currentframe",
        b"_getframe",
        b"capture_locals",
    )
    for directory in ("sqlglot", "tests"):
        for path in sorted((base / directory).rglob("*.py")):
            raw = path.read_bytes()
            tree = ast.parse(raw)
            references.visit(tree)
            if b"NotImplementedError" in raw:
                if any(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.name == "NotImplementedError"
                    for n in ast.walk(tree)
                ):
                    references.shadowed_exception = True
            if any(term in raw for term in reflective_terms) and _reads_defaults(tree):
                return {}
            if directory == "tests":
                continue
            sites = []
            decorators = {
                d.id
                for cls in tree.body
                if isinstance(cls, ast.ClassDef)
                for d in cls.decorator_list
                if isinstance(d, ast.Name)
            }
            identities = {
                name for name in decorators if _identity_decorator(base, tree, name)
            }
            for member, fn in _functions(tree, identities):
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
                            member,
                            default.lineno,
                            default.col_offset,
                            default.end_col_offset,
                            body[0].lineno,
                            body[-1].end_lineno,
                            _inert_body(fn, body),
                        ]
                    )
            if sites:
                files[path.relative_to(base).as_posix()] = [
                    hashlib.sha256(raw).hexdigest(),
                    sites,
                ]
    output = {}
    for path, (digest, entries) in files.items():
        direct = [
            [
                *site[2:-1],
                bool(site[-1])
                and not (
                    site[-1] == "NotImplementedError" and references.shadowed_exception
                ),
            ]
            for site in entries
            if site[0] not in (references.members if site[1] else references.escaped)
        ]
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
    entry = profile(base).get(patch.path)
    if not entry or not any(site[0] == number for site in entry[1]):
        return None
    if hashlib.sha256(source.encode()).hexdigest() != entry[0]:
        return None
    line = source.splitlines()[number - 1].encode()
    new = patch.apply(source).splitlines()[number - 1].encode()
    for at, begin, end, start_body, end_body, unused in entry[1]:
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
        observers = set(deps.source_readers(patch.path)) | set(deps.incomplete)
        # An early match must retain the normal bytecode-equivalence result
        # for alternate spellings. Marshalling distinguishes signed zeros.
        same_value = marshal.dumps(ast.literal_eval(old_value), 2) == marshal.dumps(
            ast.literal_eval(new_value), 2
        )
        if unused or same_value:
            return observers
        body = range(start_body, end_body + 1)
        if deps.import_initializers(patch.path, body):
            return None
        calls = set().union(*(deps.line(patch.path, n) for n in body))
        if calls:
            return calls | observers
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
