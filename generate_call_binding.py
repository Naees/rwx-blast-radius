"""Index statically resolved bare project calls and tests that may interpose a binding."""

import ast
import collections
import dis
import gzip
import hashlib
import importlib
import inspect
import unittest  # noqa: F401 - the pinned module checks for unittest at import.
from functools import lru_cache
import json
import sys
import types
from pathlib import Path

from deps import Dependencies

SKIP = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
NAMESPACE = {
    "globals",
    "exec",
    "__globals__",
    "__getattr__",
    "__setattr__",
    "__delattr__",
}
DYNAMIC_TEST = {
    "patch",
    "monkeypatch",
    "setattr",
    "delattr",
    "setitem",
    "delitem",
    "Mock",
    "MagicMock",
    "Popen",
    "run_module",
    "run_path",
    "exec",
    "eval",
    "compile",
    "__dict__",
    "__globals__",
    "_getframe",
    "currentframe",
    "getsource",
    "getsourcelines",
    "get_instructions",
    "getmembers",
    "__code__",
    "co_code",
    "settrace",
    "setprofile",
    "reload",
    "subprocess",
    "multiprocessing",
    "submit",
    "map_async",
    "starmap",
    "sys_modules",
    "f_code",
    "FunctionType",
    "vars",
    "globals",
}
SHAPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def tree_below(node):
    for obj in ast.iter_child_nodes(node):
        if not isinstance(obj, SKIP):
            yield obj
            yield from tree_below(obj)


def label(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    return None


def key(path):
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def signature(node):
    a = node.args
    if node.decorator_list or a.vararg or a.posonlyargs or node.type_params:
        return None
    return [
        None,
        node.name,
        [p.arg for p in a.args],
        0,
        [p.arg for p in a.kwonlyargs],
        len(a.defaults),
        [p.arg for p, v in zip(a.kwonlyargs, a.kw_defaults) if v is not None],
        a.kwarg is not None,
    ]


def bindings(tree, modcode):
    # STORE_NAME instructions in the module's own bytecode include conditional
    # assignments, imports, named expressions, destructuring and deletions.
    counts = collections.Counter(
        i.argval
        for i in dis.get_instructions(modcode)
        if i.opname in {"STORE_NAME", "DELETE_NAME", "STORE_GLOBAL", "DELETE_GLOBAL"}
    )
    ban = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Global):
            ban.update(n.names)
        if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names):
            return {}, {}
        if label(n) in NAMESPACE or (isinstance(n, SHAPES) and n.name in NAMESPACE):
            return {}, {}
    allowed = {k for k, v in counts.items() if v == 1} - ban
    functions, imports = {}, {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in allowed:
            shape = signature(n)
            if shape is not None:
                functions[n.name] = shape
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            for a in n.names:
                if (a.asname or a.name) in allowed:
                    imports[a.asname or a.name] = (n.module, a.name)
    return functions, imports


OUTSIDE = {
    "subprocess",
    "Popen",
    "traceback",
    "format_exc",
    "linecache",
    "run_module",
    "run_path",
    "getsource",
    "getsourcelines",
    "get_instructions",
    "__code__",
    "co_code",
    "_getframe",
    "currentframe",
    "f_code",
    "settrace",
    "setprofile",
}


def risks(tree, path, deps, labels):
    parents = {
        child: par for par in ast.walk(tree) for child in ast.iter_child_nodes(par)
    }
    result = set()
    all_in_file = {n for n in deps.names if n.split("::")[0] == path}
    for n in ast.walk(tree):
        if label(n) not in labels:
            continue
        target = n
        while target in parents and not isinstance(
            target, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            target = parents[target]
        if not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.update(all_in_file)
        else:
            enclosing = parents.get(target)
            if target.name.startswith("test_") and isinstance(
                enclosing, (ast.ClassDef, ast.Module)
            ):
                nodeid = (
                    path
                    + (
                        "::" + enclosing.name
                        if isinstance(enclosing, ast.ClassDef)
                        else ""
                    )
                    + "::"
                    + target.name
                )
                result.add(nodeid)
            else:
                result.update(deps.line(path, n.lineno))
                # Class/module fixtures may run outside per-test coverage.
                if isinstance(enclosing, ast.ClassDef) and target.name in {
                    "setUp",
                    "setUpClass",
                    "tearDown",
                    "tearDownClass",
                }:
                    result.update(
                        x
                        for x in all_in_file
                        if x.startswith(path + "::" + enclosing.name + "::")
                    )
    return result


def direct_sites(tree, code):
    by_loc = {}

    def codes(c):
        for child in c.co_consts:
            if isinstance(child, types.CodeType):
                by_loc[(child.co_firstlineno, child.co_name)] = child
                codes(child)

    codes(code)
    # A caller must itself be a top-level function or a non-nested method.
    callers = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    callers.extend(
        m
        for n in tree.body
        if isinstance(n, ast.ClassDef)
        for m in n.body
        if isinstance(m, ast.FunctionDef)
    )
    for caller in callers:
        c = by_loc.get((caller.lineno, caller.name))
        if not c or caller.decorator_list:
            continue
        forbidden = set(c.co_freevars) | set(c.co_cellvars) | set(c.co_varnames)
        for stmt in caller.body:
            if isinstance(stmt, SKIP):
                continue
            for n in (stmt, *tree_below(stmt)):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.args
                    and n.func.id not in forbidden
                    and n.func.id in c.co_names
                    and not any(isinstance(a, ast.Starred) for a in n.args)
                    and not any(k.arg is None for k in n.keywords)
                ):
                    yield n


def unsafe_project_write(tree):
    """Unknown production rebindings cannot be assigned to one test from syntax."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and label(n.func) in {"setattr", "delattr"}:
            if len(n.args) < 2 or not isinstance(n.args[1], ast.Constant):
                return True
        if (
            isinstance(n, ast.Attribute)
            and isinstance(n.ctx, (ast.Store, ast.Del))
            and n.attr in {"__code__", "__class__", "__globals__", "__dict__"}
        ):
            return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr
            in {"update", "clear", "pop", "setdefault", "__setitem__", "__delitem__"}
        ):
            if isinstance(n.func.value, ast.Attribute) and n.func.value.attr in {
                "__globals__",
                "__dict__",
            }:
                return True
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)):
            if isinstance(n.value, ast.Attribute) and n.value.attr in {
                "__globals__",
                "__dict__",
            }:
                return True
    return False


def mutations(tree):
    """Rule out known rebinding even when aliases or test setup outlive a test."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.attr)
        elif isinstance(n, ast.Call):
            fn = label(n.func)
            i = (
                0
                if fn == "patch"
                else 1
                if fn in {"setattr", "delattr", "object"}
                else None
            )
            if (
                i is not None
                and len(n.args) > i
                and isinstance(n.args[i], ast.Constant)
                and type(n.args[i].value) is str
            ):
                names.add(n.args[i].value.rsplit(".", 1)[-1])
        elif (
            isinstance(n, ast.Subscript)
            and isinstance(n.ctx, (ast.Store, ast.Del))
            and isinstance(n.slice, ast.Constant)
            and type(n.slice.value) is str
        ):
            names.add(n.slice.value)
    return names


@lru_cache(None)
def verified(caller, alias, origin, name, base):
    if "__main__" in (caller.split(".") + origin.split(".")):
        return False
    try:
        here = importlib.import_module(caller)
        there = importlib.import_module(origin)
    except (ImportError, OSError):
        return False
    fn = vars(here).get(alias)
    if (
        fn is not vars(there).get(name)
        or type(fn) is not types.FunctionType
        or fn.__module__ != origin
        or fn.__qualname__ != name
        or fn.__globals__ is not vars(there)
        or hasattr(fn, "__wrapped__")
        or hasattr(fn, "__signature__")
    ):
        return False
    code = fn.__code__
    if not Path(code.co_filename).resolve().is_relative_to(base / "sqlglot"):
        return False
    if set(code.co_names) & {
        "_getframe",
        "currentframe",
        "f_back",
        "getframeinfo",
        "exec",
        "eval",
    }:
        return False
    return not code.co_flags & (
        inspect.CO_VARARGS
        | inspect.CO_GENERATOR
        | inspect.CO_COROUTINE
        | inspect.CO_ASYNC_GENERATOR
    )


def build(base, deps):
    base = Path(base).resolve()
    sys.path.insert(0, str(base))
    modules = {}
    raw = {}
    dangerous = set(deps.incomplete)
    outside = set(deps.incomplete)
    written = set()
    for directory in ("sqlglot", "tests"):
        for file in sorted((base / directory).rglob("*.py")):
            path = file.relative_to(base).as_posix()
            source = file.read_bytes()
            tree = ast.parse(source)
            written.update(mutations(tree))
            if directory == "tests":
                dangerous.update(risks(tree, path, deps, DYNAMIC_TEST))
                outside.update(risks(tree, path, deps, OUTSIDE))
                continue
            if unsafe_project_write(tree):
                return {"files": {}, "uncertain": [], "outside": []}
            code = compile(tree, str(file), "exec", dont_inherit=True)
            fns, imports = bindings(tree, code)
            modules[key(path)] = fns
            if fns or imports:
                raw[path] = (
                    hashlib.sha256(source).hexdigest(),
                    fns,
                    imports,
                    [
                        (n.lineno, n.col_offset, n.func.id)
                        for n in direct_sites(tree, code)
                    ],
                )
    result = {}
    for path, (digest, fns, imports, rows) in raw.items():
        sites = {}
        for line, col, name in rows:
            origin, export = imports.get(name, (key(path), name))
            if name in written or export in written:
                continue
            shape = modules.get(origin, {}).get(export)
            if shape is not None and verified(key(path), name, origin, export, base):
                sites[f"{line}:{col}"] = [name, origin, export, shape]
        if sites:
            result[path] = [digest, sites]
    return {
        "files": result,
        "uncertain": sorted(dangerous & deps.tests),
        "outside": sorted(outside & deps.tests),
    }


if __name__ == "__main__":
    data = build(Path(sys.argv[1]), Dependencies())
    with (
        Path(sys.argv[2]).open("wb") as f,
        gzip.GzipFile(fileobj=f, mode="wb", mtime=0, filename="") as z,
    ):
        z.write(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    print(
        len(data["files"]),
        sum(len(r[1]) for r in data["files"].values()),
        len(data["uncertain"]),
    )
