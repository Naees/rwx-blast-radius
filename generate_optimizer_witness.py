"""Profile local expression inputs in constructors and ordinary function calls."""

import ast
import builtins
import concurrent.futures
import dis
import gzip
import importlib.machinery
import inspect
import json
import os
import subprocess
import sys
import types
import unittest
import tempfile
import multiprocessing.util
from collections import Counter
from functools import lru_cache
from pathlib import Path

from optimizer_replay import TARGETS

HERE = Path(__file__).resolve().parent

active = None
seen = {}
unsafe = set()
identities = {}
MAX_TYPES = 12
expected_workers = Counter()


def entry(site):
    if active is None:
        unsafe.add(site)
        return None
    return seen.setdefault(site, {}).setdefault(active, {})


def descriptor(cls, name):
    for typ in type.__getattribute__(cls, "__mro__"):
        ns = type.__getattribute__(typ, "__dict__")
        if name in ns:
            return ns[name]


def type_name(cls):
    meta = type(cls)
    if (
        descriptor(meta, "__getattribute__") is type.__getattribute__
        and descriptor(meta, "__name__") is vars(type)["__name__"]
    ):
        return type.__getattribute__(cls, "__name__")
    return None


@lru_cache(None)
def getter_field(body):
    code = [
        i
        for i in dis.get_instructions(body)
        if i.opname not in {"RESUME", "CACHE", "NOP"}
    ]
    if [x.opname for x in code] == ["LOAD_FAST", "LOAD_ATTR", "RETURN_VALUE"]:
        if body.co_argcount == 1 and code[0].argval == body.co_varnames[0]:
            return code[1].argval


def simple(value):
    return [type_name(type(value)), value is None]


def attributes(value, names):
    attrs = {"": simple(value)}
    cls = type(value)
    if (
        not names
        or not type.__getattribute__(cls, "__module__").startswith("sqlglot.")
        or descriptor(cls, "__getattribute__") is not object.__getattribute__
    ):
        return attrs
    desc = descriptor(cls, "__dict__")
    if not isinstance(desc, types.GetSetDescriptorType):
        return attrs
    data = desc.__get__(value, cls)
    if type(data) is not dict:
        return attrs
    for key in names:
        current = descriptor(cls, key)
        if not inspect.isdatadescriptor(current) and key in data:
            attrs[key] = simple(data[key])
        elif type(current) is property and type(current.fget) is types.FunctionType:
            target = getter_field(current.fget.__code__)
            if (
                target
                and target in data
                and not inspect.isdatadescriptor(descriptor(cls, target))
            ):
                attrs[key] = simple(data[target])
    return attrs


def value(site, obj, names):
    rec = entry(site)
    if rec is None or rec.get("blocked"):
        return obj
    frame = sys._getframe(1)
    symbols = (
        *frame.f_code.co_varnames,
        *frame.f_code.co_freevars,
        *frame.f_code.co_cellvars,
    )
    if (
        "<locals>" in frame.f_code.co_qualname
        or "type" in symbols
        or frame.f_globals.get("type", frame.f_builtins.get("type")) is not type
    ):
        rec["blocked"] = True
        return obj
    try:
        current = attributes(obj, names)
    except Exception:
        rec["blocked"] = True
        return obj
    if "paths" not in rec:
        rec["paths"] = {k: {tuple(v)} for k, v in current.items()}
    else:
        paths = rec["paths"]
        for k in list(paths):
            if k not in current:
                del paths[k]
            else:
                paths[k].add(tuple(current[k]))
                if len(paths[k]) > MAX_TYPES:
                    del paths[k]
    return obj


@lru_cache(None)
def code_shape(code, module, qualname, defaults, kw_defaults):
    if not Path(code.co_filename).is_relative_to(base / "sqlglot"):
        return None
    if set(code.co_names).intersection(
        {"currentframe", "_getframe", "f_back", "getframeinfo", "exec", "eval"}
    ):
        return None
    positional = code.co_varnames[: code.co_argcount]
    kwonly = code.co_varnames[
        code.co_argcount : code.co_argcount + code.co_kwonlyargcount
    ]
    if code.co_flags & (
        inspect.CO_VARARGS
        | inspect.CO_GENERATOR
        | inspect.CO_COROUTINE
        | inspect.CO_ASYNC_GENERATOR
    ):
        return None
    return (
        module,
        qualname,
        positional,
        code.co_posonlyargcount,
        kwonly,
        defaults,
        kw_defaults,
        bool(code.co_flags & inspect.CO_VARKEYWORDS),
    )


def function_shape(fn):
    if (
        type(fn) is not types.FunctionType
        or hasattr(fn, "__wrapped__")
        or hasattr(fn, "__signature__")
    ):
        return None
    module = sys.modules.get(fn.__module__)
    if (
        not fn.__module__.startswith("sqlglot.")
        or "." in fn.__qualname__
        or module is None
        or vars(module).get(fn.__qualname__) is not fn
    ):
        return None
    return code_shape(
        fn.__code__,
        fn.__module__,
        fn.__qualname__,
        len(fn.__defaults__ or ()),
        tuple((fn.__kwdefaults__ or {}).keys()),
    )


def callable_site(site, fn):
    rec = entry(site)
    if rec is not None and not rec.get("blocked"):
        shape = function_shape(fn) if type(fn) is types.FunctionType else None
        actual = (fn, fn.__code__) if shape else (None, None)
        original = identities.setdefault((site, active), actual)
        if (
            shape is None
            or original[0] is not fn
            or original[1] is not fn.__code__
            or ("shape" in rec and rec["shape"] != shape)
        ):
            rec["blocked"] = True
        else:
            rec["shape"] = shape
    return fn


class Transform(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path
        self.fn = None
        self.parameters = set()

    def visit_FunctionDef(self, n):
        previous = self.fn, self.parameters
        self.fn = n
        self.parameters = {
            a.arg for a in [*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs]
        }
        self.parameters -= {
            a.id
            for a in ast.walk(n)
            if isinstance(a, ast.Name) and isinstance(a.ctx, ast.Del)
        }
        self.generic_visit(n)
        self.fn, self.parameters = previous
        return n

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, n):
        if self.fn and len(n.targets) == 1:
            target = n.targets[0]
            rhs = n.value
            site = f"{self.path}:{rhs.lineno}:{rhs.col_offset}"
            if (
                self.fn.name == "__init__"
                and isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and isinstance(rhs, ast.Name)
                and rhs.id in self.parameters
            ):
                fields = tuple(
                    sorted(
                        {
                            a.attr
                            for a in ast.walk(self.fn)
                            if isinstance(a, ast.Attribute)
                            and isinstance(a.value, ast.Name)
                            and a.value.id == rhs.id
                            and not a.attr.startswith("_")
                        }
                    )
                )
                n.value = ast.copy_location(
                    ast.Call(
                        ast.Name("_br_witness_value", ast.Load()),
                        [ast.Constant(site), rhs, ast.Constant(fields)],
                        [],
                    ),
                    rhs,
                )
            elif isinstance(rhs, ast.Call) and isinstance(rhs.func, ast.Name):
                rhs.func = ast.copy_location(
                    ast.Call(
                        ast.Name("_br_witness_call", ast.Load()),
                        [ast.Constant(site), rhs.func],
                        [],
                    ),
                    rhs.func,
                )
        return n


class Finder:
    def find_spec(self, name, path=None, target=None):
        if not name.startswith("sqlglot.optimizer."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.endswith(".py"):

            class Loader(importlib.machinery.SourceFileLoader):
                witness_instrumented = True

                def get_code(self, fullname):
                    tree = ast.parse(self.get_data(self.path), self.path)
                    tr = Transform(str(Path(self.path).relative_to(base)))
                    return compile(
                        ast.fix_missing_locations(tr.visit(tree)), self.path, "exec"
                    )

            spec.loader = Loader(name, spec.origin)
        return spec


def install():
    builtins._br_witness_value, builtins._br_witness_call = value, callable_site
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    sys.meta_path.insert(0, Finder())


def encoded():
    rows = {}
    for site, methods in seen.items():
        for node, rec in methods.items():
            if "paths" in rec:
                rec = {
                    "paths": {k: sorted(v, key=repr) for k, v in rec["paths"].items()},
                    "blocked": rec.get("blocked", False),
                }
            rows.setdefault(site, {})[node] = rec
    return {"sites": rows, "unsafe": sorted(unsafe)}


def merge(dest, part):
    for site in part["unsafe"]:
        unsafe.add(site)
    for site, methods in part["sites"].items():
        for node, entry in methods.items():
            cells = dest.setdefault(site, {})
            if node not in cells:
                cells[node] = entry
                continue
            rec = cells[node]
            if rec.get("blocked") or entry.get("blocked"):
                rec["blocked"] = True
            elif "shape" in rec:
                if rec["shape"] != entry.get("shape"):
                    rec["blocked"] = True
            elif "paths" in rec and "paths" in entry:
                paths = rec["paths"]
                for k in list(paths):
                    if k not in entry["paths"]:
                        del paths[k]
                    else:
                        union = {tuple(x) for x in [*paths[k], *entry["paths"][k]]}
                        if len(union) > MAX_TYPES:
                            del paths[k]
                        else:
                            paths[k] = sorted(union, key=repr)
            else:
                rec["blocked"] = True


def flush_worker():
    out = Path(os.environ["WITNESS_PARTS"]) / f"{os.getpid()}.json"
    raw = encoded()
    raw["worker_test"] = os.environ["WITNESS_ACTIVE"]
    raw["instrumented"] = all(
        getattr(mod.__spec__.loader, "witness_instrumented", False)
        for name, mod in sys.modules.items()
        if name.startswith("sqlglot.optimizer.") and mod is not None
    )
    out.write_text(json.dumps(raw, separators=(",", ":")))


class SerialSpawn(concurrent.futures.ProcessPoolExecutor):
    def __init__(self, *args, **kwargs):
        expected_workers[active] += 1
        super().__init__(*args, **{**kwargs, "max_workers": 1})


if __name__ == "__mp_main__" and "WITNESS_ACTIVE" in os.environ:
    base = Path(os.environ["WITNESS_BASE"])
    active = os.environ["WITNESS_ACTIVE"]
    install()
    multiprocessing.util.Finalize(None, flush_worker, exitpriority=1)


if __name__ == "__main__":
    base = Path(sys.argv[1]).resolve()
    output = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) > 2
        else HERE / "maps/optimizer_witness.json.gz"
    )
    if (
        subprocess.check_output(
            ["git", "-C", base, "rev-parse", "HEAD"], text=True
        ).strip()
        != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
    ):
        raise ValueError("Unexpected checkout")
    subprocess.run(["git", "-C", base, "diff", "--quiet", "HEAD"], check=True)
    install()
    concurrent.futures.ProcessPoolExecutor = SerialSpawn
    os.environ["WITNESS_BASE"] = str(base)
    os.chdir(base)
    os.environ["SKIP_INTEGRATION"] = "0"
    from tests import test_executor, test_optimizer

    rows = {}
    with tempfile.TemporaryDirectory() as directory:
        os.environ["WITNESS_PARTS"] = directory
        for node in TARGETS:
            active = os.environ["WITNESS_ACTIVE"] = node
            path, cls, name = node.split("::")
            mod = test_executor if path.endswith("test_executor.py") else test_optimizer
            report = unittest.TestSuite([getattr(mod, cls)(name)]).run(
                unittest.TestResult()
            )
            active = None
            if not report.wasSuccessful() or report.skipped:
                raise RuntimeError((node, report.errors[:1], report.failures[:1]))
            print(node, len(seen), flush=True)
        merge(rows, json.loads(json.dumps(encoded())))
        parts = sorted(Path(directory).glob("*.json"))
        if len(parts) < 2:
            raise RuntimeError("Worker traces missing")
        worker_tests = set()
        completed_workers = Counter()
        for file in parts:
            part = json.loads(file.read_bytes())
            if not part.get("instrumented") or not part.get("worker_test"):
                raise RuntimeError("Uninstrumented executor worker")
            completed_workers[part["worker_test"]] += 1
            worker_tests.update(n for group in part["sites"].values() for n in group)
            merge(rows, part)
        if completed_workers != expected_workers:
            raise RuntimeError("Incomplete worker output")
        # The TPC-H executor workers only deserialize expressions, and may
        # never enter the optimizer. The DS workers must actually run it.
        if TARGETS[0] not in worker_tests:
            raise RuntimeError("Executor worker optimizer traces missing")
    final = {
        site: {n: rec for n, rec in bytest.items() if not rec.get("blocked")}
        for site, bytest in rows.items()
        if site not in unsafe
    }
    from deps import Dependencies

    deps = Dependencies()
    for site, tests in list(final.items()):
        path, lineno, _ = site.rsplit(":", 2)
        if deps.import_initializers(path, {int(lineno)}):
            del final[site]
            continue
        for node in deps.source_readers(path):
            tests.pop(node, None)
    final = {site: tests for site, tests in final.items() if tests}
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps(final, separators=(",", ":")).encode())
    print(
        "Verified",
        len(final),
        "sites across all five complete tests and",
        len(parts),
        "spawned workers",
    )
