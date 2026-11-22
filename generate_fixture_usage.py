"""Observe per-test reads of rows yielded by SQLGlot's pinned fixture loaders."""

import ast
import builtins
import concurrent.futures
import gzip
import hashlib
import importlib.machinery
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from deps import Dependencies
from generate_map import PINNED, Recorder, TracedPool

FIXTURES = "tests/fixtures/"


class Text(str):
    def __new__(cls, value, filename, index, field):
        obj = super().__new__(cls, value)
        obj.site = (filename, index, field)
        return obj

    def __reduce__(self):
        return Text, (str(self), *self.site)


class Metadata(dict):
    def __init__(self, entries, filename, index):
        super().__init__(entries)
        self.site = (filename, index)

    def read(self, key):
        profile.record((*self.site, "meta:" + key))

    def get(self, key, *args):
        self.read(key)
        return super().get(key, *args)

    def __getitem__(self, key):
        self.read(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read(key)
        return super().__contains__(key)

    def __len__(self):
        self.read("*")
        return super().__len__()

    def __iter__(self):
        self.read("*")
        return super().__iter__()

    def items(self):
        self.read("*")
        return super().items()

    def keys(self):
        self.read("*")
        return super().keys()

    def values(self):
        self.read("*")
        return super().values()

    def __reduce__(self):
        return Metadata, (dict(dict.items(self)), *self.site)


def use(value, purpose):
    if isinstance(value, Text):
        profile.record((*value.site, purpose))
        if purpose == "cache":
            profile.cache[hashlib.sha256(value.encode()).hexdigest()].add(
                value.site[:2]
            )
    return value


class Uses(ast.NodeTransformer):
    """Record actual uses, including values unpacked in earlier helper calls."""

    def __init__(self, module):
        self.module = module
        self.function = None
        self.purpose = "read"

    def visit_FunctionDef(self, node):
        saved, self.function = self.function, node.name
        self.generic_visit(node)
        self.function = saved
        return node

    def visit_Call(self, node):
        if (
            self.module == "tests.test_executor"
            and self.function == "_mp_execute"
            and isinstance(node.func, ast.Name)
            and node.func.id == "parse_one"
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Name)
        ):
            # _mp_execute parses even the rows whose worker immediately skips.
            arg = node.args[0]
            node.args[0] = self.wrap(arg, "parse")
            return node
        if self.module == "tests.test_executor" and isinstance(
            node.func, ast.Attribute
        ):
            if node.func.attr == "cached_execute" and isinstance(
                node.args[0], ast.Name
            ):
                node.args[0] = self.wrap(node.args[0], "cache")
                return node
            if node.func.attr == "subTest":
                saved, self.purpose = self.purpose, "label"
                self.generic_visit(node)
                self.purpose = saved
                return node
        if (
            self.module == "tests.test_executor"
            and isinstance(node.func, ast.Name)
            and node.func.id == "transpile"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and len(node.keywords) == 1
            and node.keywords[0].arg == "write"
            and isinstance(node.keywords[0].value, ast.Constant)
            and node.keywords[0].value.value == "duckdb"
        ):
            node.args[0] = self.wrap(node.args[0], "duckdb")
            return node
        return self.generic_visit(node)

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return node
        # The other loads of sql in cached_execute are dictionary key operations.
        if (
            self.module == "tests.test_executor"
            and self.function == "cached_execute"
            and node.id == "sql"
        ):
            return self.wrap(node, "cache")
        return self.wrap(node, self.purpose)

    @staticmethod
    def wrap(node, purpose):
        wrapped = ast.Call(
            ast.Name("_br_fixture_use", ast.Load()), [node, ast.Constant(purpose)], []
        )
        return ast.copy_location(wrapped, node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = Uses(fullname).visit(ast.parse(source, self.path))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in modules:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and Path(spec.origin).is_relative_to(root):
                spec.loader = Loader(fullname, spec.origin)
            return spec
        return None


class Profile(Recorder):
    def __init__(self, root, nodes):
        super().__init__(root, nodes)
        self.fields = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        self.raw = defaultdict(int)
        self.decoding = None
        self.lengths = {}
        self.optimizer = defaultdict(int)
        self.cache = defaultdict(set)
        self.unowned = set()

    def activate(self, mask):
        self.active = mask

    def record(self, site):
        file, row, field, *purpose = site
        key = field if not purpose else field + ":" + purpose[0]
        self.fields[file][row][key] |= self.active

    def audit(self, event, args):
        if event == "open" and isinstance(args[0], str) and args[0].endswith(".sql"):
            name = self.relative(os.path.abspath(args[0]))
            if (
                name
                and name.startswith(FIXTURES)
                and name.endswith(".sql")
                and self.decoding != name
            ):
                if self.active:
                    self.raw[name] |= self.active
                else:
                    self.unowned.add(name)


def rows(filename, original):
    path = FIXTURES + filename
    caller = sys._getframe(2)
    optimizer = (
        caller.f_code.co_qualname == "TestOptimizer.check_file"
        and not caller.f_locals.get("only")
    )
    i = 0
    iterator = original(filename)
    while True:
        prev = profile.decoding
        try:
            profile.decoding = path
            meta, sql, expected = next(iterator)
        except StopIteration:
            profile.lengths[path] = i
            break
        finally:
            profile.decoding = prev
        if optimizer:
            profile.optimizer[path] |= profile.active
        yield (
            Metadata(meta, path, i),
            Text(sql, path, i, "sql"),
            Text(expected, path, i, "expected"),
        )
        i += 1


def main():
    global root, modules, profile
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PINNED
        or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode
    ):
        raise ValueError("Expected clean pinned base")
    deps = Dependencies(output.parent / "deps.json.gz")
    profile = Profile(
        root, sorted(row["id"] for row in json.loads(inventory.read_bytes())["tests"])
    )
    paths = [p.relative_to(root).as_posix() for p in (root / FIXTURES).rglob("*.sql")]
    profile.only = set().union(*(deps.fixture(p) for p in paths))
    modules = {
        n.split("::")[0].removesuffix(".py").replace("/", ".") for n in profile.only
    }
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    builtins._br_fixture_use = use
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(profile.audit)
    from tests import helpers

    original = helpers.load_sql_fixture_pairs

    def paired(filename):
        yield from rows(filename, original)

    helpers.load_sql_fixture_pairs = paired
    concurrent.futures.ProcessPoolExecutor = TracedPool
    # Preload before pytest assertion rewriting picks the source loader.
    for mod in sorted(modules):
        importlib.import_module(mod)
    rc = pytest.main(
        ["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[profile]
    )
    if rc not in (0, 1):
        raise SystemExit(rc)
    known = profile.passed - profile.failed
    pool = {}

    def intern(bits):
        return pool.setdefault(bits, len(pool))

    files = {}
    for path, count in profile.lengths.items():
        if not count or path in profile.unowned:
            continue
        rows_data = []
        for i in range(count):
            fields = {
                k: intern(v) for k, v in sorted(profile.fields[path][i].items()) if v
            }
            rows_data.append(fields)
        if not any(rows_data):
            continue
        files[path] = {
            "rows": rows_data,
            "raw": intern(profile.raw[path]),
            "optimizer": intern(profile.optimizer[path]),
            "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
        }
    data = {
        "tests": profile.tests,
        "pool": [hex(b) for b in pool],
        "files": files,
        "unknown": sorted(profile.only - known),
        "cache": {key: len(rows) for key, rows in sorted(profile.cache.items())},
    }
    with (
        open(output, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
    ):
        gz.write(json.dumps(data, separators=(",", ":")).encode())
    print(
        "Profiled",
        len(files),
        "fixtures against",
        len(known),
        "/",
        len(profile.only),
        "tests",
    )


if __name__ == "__main__":
    main()
