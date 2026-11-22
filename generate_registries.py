"""Follow read dependencies of literal registry entries on the pinned base.

Inherited/copied mappings carry source-site numbers along with each key. Values
are untouched so callback identity, enum values, and expression classes retain
their baseline behaviour. Untracked construction-time dependencies fall back.
"""

import argparse
import ast
import builtins
import concurrent.futures
import gzip
import importlib.machinery
import inspect
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from configmap import members, module_members
from generate_map import PINNED, Recorder, TracedPool


class Table(dict):
    def __init__(self):
        super().__init__()
        self.origins = {}
        self.bulk_cache = None

    def record(self, key):
        for site in self.origins.get(key, ()):
            profile.hit(site)

    def __getitem__(self, key):
        self.record(key)
        return dict.__getitem__(self, key)

    def get(self, key, *default):
        self.record(key)
        return dict.get(self, key, *default)

    def __iter__(self):
        # Prevent CPython's raw-dict fast copy from silently dropping origins.
        return dict.__iter__(self)

    def setdefault(self, key, *default):
        self.record(key)
        return dict.setdefault(self, key, *default)

    def pop(self, key, *default):
        self.record(key)
        self.origins.pop(key, None)
        self.bulk_cache = None
        return dict.pop(self, key, *default)

    def __setitem__(self, key, value):
        self.origins.pop(key, None)
        self.bulk_cache = None
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        self.origins.pop(key, None)
        self.bulk_cache = None
        dict.__delitem__(self, key)

    def update(self, *args, **kwargs):
        if args:
            other = args[0]
            if isinstance(other, Table):
                for key, value in dict.items(other):
                    dict.__setitem__(self, key, value)
                    self.origins[key] = other.origins.get(key, ())
            else:
                for key, value in dict(other).items():
                    self[key] = value
        for key, value in kwargs.items():
            self[key] = value
        self.bulk_cache = None

    def copy(self):
        out = Table()
        out.update(self)
        return out

    def __or__(self, other):
        out = self.copy()
        out.update(other)
        return out

    def __ror__(self, other):
        out = Table()
        out.update(other)
        out.update(self)
        return out

    def __ior__(self, other):
        self.update(other)
        return self

    def bulk(self):
        # Unknown consumers can copy values or derive persistent caches. Mark
        # sites unsupported outside of a concrete test's own assertion code.
        if self.bulk_cache is None:
            self.bulk_cache = set().union(*self.origins.values())
        frame = sys._getframe(2)
        test_only = (
            "/tests/" in frame.f_code.co_filename and frame.f_code.co_name != "<module>"
        )
        for site in self.bulk_cache:
            if test_only:
                profile.hit(site)
            else:
                profile.unsafe.add(site)

    def items(self):
        self.bulk()
        return dict.items(self)

    def values(self):
        self.bulk()
        return dict.values(self)

    def keys(self):
        # CPython's uninstrumented dict.update, f(**mapping), and dict copies
        # can consult keys() before transferring values to a long-lived cache.
        self.bulk()
        return dict.keys(self)

    def __eq__(self, other):
        self.bulk()
        return dict.__eq__(self, other)

    def __ne__(self, other):
        self.bulk()
        return dict.__ne__(self, other)

    def __reduce__(self):
        self.bulk()
        return dict, (dict(dict.items(self)),)


def literal_table(entries):
    out = Table()
    tracked = False
    for site, entry in entries:
        if len(entry) == 1:
            source = entry[0]
            if isinstance(source, Table):
                out.update(source)
                tracked = True
            else:
                out.update(source)
            if site:
                for key in source:
                    out.origins[key] = (*out.origins.get(key, ()), site)
        else:
            key, value = entry
            out[key] = value
            if site:
                out.origins[key] = (site,)
        tracked |= bool(site)
    return out if tracked else dict(dict.items(out))


def filtered(mapping, predicate):
    if not isinstance(mapping, Table):
        return {k: v for k, v in mapping.items() if predicate(k)}
    result = Table()
    for key, value in dict.items(mapping):
        if predicate(key):
            dict.__setitem__(result, key, value)
            result.origins[key] = mapping.origins.get(key, ())
    return result


def make_copy(mapping):
    return mapping.copy() if isinstance(mapping, Table) else dict(mapping)


class Profile(Recorder):
    def __init__(self, root, nodes):
        super().__init__(root, nodes)
        self.loading = 0
        self.index = {}
        self.masks = defaultdict(int)
        self.unsafe = set()
        self.current = set()
        self.namespaces = defaultdict(set)

    def activate(self, mask):
        for site in self.current:
            self.masks[site] |= self.active
        self.current.clear()
        self.active = mask

    def hit(self, site):
        if self.active and not self.loading:
            self.current.add(site)
        else:
            self.unsafe.add(site)

    def site(self, path, node):
        key = f"{path}:{node.lineno}:{node.col_offset}"
        return self.index.setdefault(key, len(self.index) + 1)


class Transform(ast.NodeTransformer):
    def __init__(self, path, fullname, tree):
        self.path = path
        defs = [*members(tree, fullname), *module_members(tree, fullname)]
        self.tables = {
            st.value
            for sym, st in defs
            if sym.rsplit(".", 1)[-1].isupper()
            and isinstance(st.value, (ast.Dict, ast.DictComp))
        }
        self.fullname = fullname

    @staticmethod
    def call(name, args, node):
        return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), args, []), node)

    def visit_Attribute(self, node):
        # The resolver accepts changed dotted references only when their
        # parent namespace exists in the original module and the member is defined.
        if isinstance(node.ctx, ast.Load):
            path = dotted(node)
            if path and len(path.split(".")) in (2, 3):
                profile.namespaces[self.fullname].add(path.rsplit(".", 1)[0])
        return self.generic_visit(node)

    def visit_Call(self, node):
        self.generic_visit(node)
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and len(node.args) == 1
            and not node.keywords
            and not isinstance(node.args[0], ast.Starred)
        ):
            return self.call("_br_copy_mapping", node.args, node)
        return node

    def visit_DictComp(self, node):
        self.generic_visit(node)
        result = node
        if len(node.generators) == 1:
            g = node.generators[0]
            target = g.target
            it = g.iter
            if (
                not g.is_async
                and isinstance(target, ast.Tuple)
                and len(target.elts) == 2
                and all(isinstance(x, ast.Name) for x in target.elts)
                and isinstance(node.key, ast.Name)
                and isinstance(node.value, ast.Name)
                and [x.id for x in target.elts] == [node.key.id, node.value.id]
                and isinstance(it, ast.Call)
                and not it.args
                and not it.keywords
                and isinstance(it.func, ast.Attribute)
                and it.func.attr == "items"
                and all(
                    node.value.id
                    not in {x.id for x in ast.walk(cond) if isinstance(x, ast.Name)}
                    for cond in g.ifs
                )
            ):
                test = (
                    ast.BoolOp(ast.And(), g.ifs)
                    if len(g.ifs) > 1
                    else (g.ifs[0] if g.ifs else ast.Constant(True))
                )
                args = ast.arguments([], [ast.arg(node.key.id)], None, [], [], None, [])
                result = self.call(
                    "_br_filter_mapping", [it.func.value, ast.Lambda(args, test)], node
                )
        if node in self.tables:
            site = profile.site(self.path, node.value)
            entry = ast.Tuple(
                [ast.Constant(site), ast.Tuple([result], ast.Load())], ast.Load()
            )
            result = self.call(
                "_br_literal_mapping", [ast.List([entry], ast.Load())], node
            )
        return result

    def visit_Dict(self, node):
        tracked = node in self.tables
        if not tracked and None not in node.keys:
            return self.generic_visit(node)
        entries = []
        for key, value in zip(node.keys, node.values):
            site = 0
            if tracked and (key is not None or isinstance(value, ast.DictComp)):
                site = profile.site(self.path, value.value if key is None else value)
            obj = self.visit(value)
            pair = [obj] if key is None else [self.visit(key), obj]
            entries.append(
                ast.Tuple([ast.Constant(site), ast.Tuple(pair, ast.Load())], ast.Load())
            )
        return self.call("_br_literal_mapping", [ast.List(entries, ast.Load())], node)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return parent + "." + node.attr if parent else None
    return None


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        # A lazy import can occur during a test but retain its derived
        # configuration for every subsequent test.
        profile.loading += 1
        try:
            return super().exec_module(module)
        finally:
            profile.loading -= 1

    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        base = Path(self.path).relative_to(root).as_posix()
        tree = Transform(base, fullname, tree).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if (
                spec
                and spec.origin
                and spec.origin.endswith(".py")
                and Path(spec.origin).is_relative_to(root)
            ):
                spec.loader = Loader(fullname, spec.origin)
            return spec
        return None


def safe_members():
    pool = []
    indices = {}
    allowed = {}
    for name, prefixes in profile.namespaces.items():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        allowed[name] = {}
        for prefix in prefixes:
            parts = prefix.split(".")
            obj = vars(mod).get(parts[0])
            for attr in parts[1:]:
                obj = vars(obj).get(attr) if hasattr(obj, "__dict__") else None
            if obj is None or not (inspect.isclass(obj) or inspect.ismodule(obj)):
                continue
            names = tuple(sorted(n for n in vars(obj) if not n.startswith("_")))
            if names not in indices:
                indices[names] = len(pool)
                pool.append(names)
            allowed[name][prefix] = indices[names]
    return allowed, pool


def main():
    global root, profile
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--min-cost", type=float)
    args = parser.parse_args()
    root, inventory, output = [
        p.resolve() for p in (args.root, args.inventory, args.output)
    ]
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PINNED
        or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode
    ):
        raise ValueError("Expected clean pinned base")
    costs = {t["id"]: t["seconds"] for t in json.loads(inventory.read_bytes())["tests"]}
    profile = Profile(root, sorted(costs))
    if args.max_cost is not None:
        profile.only = {n for n in profile.only if costs[n] <= args.max_cost}
    if args.min_cost is not None:
        profile.only = {n for n in profile.only if costs[n] > args.min_cost}
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    sys.path.insert(0, str(root))
    builtins._br_literal_mapping = literal_table
    builtins._br_copy_mapping = make_copy
    builtins._br_filter_mapping = filtered
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.meta_path.insert(0, Finder())
    rc = pytest.main(
        ["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[profile]
    )
    if rc not in (0, 1):
        raise SystemExit(rc)
    done = profile.passed - profile.failed
    allowed, pool = safe_members()
    masks = {
        site: hex(profile.masks[idx])
        for site, idx in profile.index.items()
        if idx not in profile.unsafe
    }
    data = {
        "tests": profile.tests,
        "done": sorted(done),
        "masks": masks,
        "unsafe": [s for s, i in profile.index.items() if i in profile.unsafe],
        "names": allowed,
        "pool": pool,
    }
    with (
        open(output, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
    ):
        gz.write(json.dumps(data, separators=(",", ":")).encode())
    print(
        f"Profiled {len(done)}/{len(profile.only)} tests, {len(masks)} supported entries and {len(profile.unsafe)} untracked derivations"
    )


if __name__ == "__main__":
    main()
