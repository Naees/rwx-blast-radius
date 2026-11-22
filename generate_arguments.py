"""Record argument presence during validation on the pinned base.

Select a bounded cohort covering sparsely exercised expression configurations.
Unmeasured validations and unclassified value reads retain the broader profile.
"""
import ast
import concurrent.futures
import functools
import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from deps import Dependencies
from generate_map import PINNED, Recorder, TracedPool


def key_only(child, parent):
    if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is child:
        return True
    if isinstance(parent, ast.Compare) and len(parent.ops) == 1:
        return parent.comparators[0] is child and isinstance(parent.ops[0], (ast.In, ast.NotIn))
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
        return parent.func.id in {"tuple", "list", "len", "set", "zip", "sorted", "iter"}
    return isinstance(parent, ast.Attribute) and parent.value is child and parent.attr == "keys"


def value_consumers(root):
    readers = defaultdict(set)
    validators = set()
    for path in sorted([*(root / "sqlglot").rglob("*.py"), *(root / "tests").rglob("*.py")]):
        source = path.read_text()
        if "arg_types" not in source and "required_args" not in source:
            continue
        tree = ast.parse(source)
        parents = {child: n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}
        rel = path.relative_to(root).as_posix()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Attribute) or not isinstance(n.ctx, ast.Load) or n.attr not in {"required_args", "arg_types"}:
                continue
            parent = parents[n]
            if n.attr == "arg_types" and key_only(n, parent):
                continue
            ancestor = parent
            while ancestor in parents and not isinstance(ancestor, (ast.FunctionDef, ast.ClassDef)):
                ancestor = parents[ancestor]
            fn = getattr(ancestor, "name", None)
            if rel == "sqlglot/expressions/core.py" and fn == "__init_subclass__" and n.attr == "arg_types":
                # This comprehension materializes the cached required_args set.
                stmt = next(s for s in ancestor.body if s.lineno <= n.lineno <= s.end_lineno)
                if ast.unparse(stmt) == "cls.required_args = {k for k, v in cls.arg_types.items() if v}":
                    continue
            if rel == "sqlglot/expressions/core.py" and fn == "error_messages" and n.attr == "required_args":
                if not isinstance(parent, ast.For) or ast.unparse(parent.body[0]) != "v = self.args.get(k)":
                    raise ValueError("Unrecognized validation loop")
                if ast.unparse(parent.body[1].test) != "v is None or (isinstance(v, list) and (not v))":
                    raise ValueError("Unrecognized argument presence condition")
                validators.add((rel, n.lineno))
                continue
            if isinstance(parent, ast.IfExp):
                stmt = parents[parent]
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    name = stmt.targets[0].id
                    uses = [x for x in ast.walk(ancestor) if isinstance(x, ast.Name) and x.id == name and isinstance(x.ctx, ast.Load)]
                    if uses and all(key_only(x, parents[x]) for x in uses):
                        continue
            # Restrict an explicitly named class consumer to that class. This
            # includes class declarations that copy a parent's arg_types dictionary.
            cls = ast.unparse(n.value).split(".")[-1]
            scope = cls if cls[0].isupper() else "*"
            readers[scope].add((rel, n.lineno))
    if not validators:
        raise ValueError("No validated expression dependency found")
    return readers, validators


def mask_for(sites, deps, bits):
    tests = set()
    for f, line in sites:
        tests.update(deps.line(f, line))
    return sum(bits[t] for t in tests)


def main():
    root, inventory, output = [Path(a).resolve() for a in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Expected pinned base")
    if subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Dirty base checkout")
    config = json.loads(gzip.decompress((output.parent / "config.json.gz").read_bytes()))
    deps = Dependencies(output.parent / "deps.json.gz")
    nodes = config["tests"]
    costs = {t["id"]: t["seconds"] for t in json.loads(inventory.read_bytes())["tests"]}
    profile = Recorder(root, nodes)
    profile.activate = lambda mask: setattr(profile, "active", mask)
    cohort = 0
    for symbol, mask in config["masks"].items():
        count = int(mask, 16).bit_count()
        if symbol.endswith(".arg_types") and count <= 16:
            cohort |= int(mask, 16)
    profile.only = {n for n in nodes if cohort & profile.bits[n] and costs[n] < 2}
    readers, sites = value_consumers(root)
    unknown = {cls:hex(mask_for(uses, deps, profile.bits)) for cls, uses in readers.items()}
    validator = mask_for(sites, deps, profile.bits)

    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = TracedPool
    from sqlglot.expressions.core import Expression
    validate = Expression.error_messages
    missing = defaultdict(int)

    @functools.lru_cache(None)
    def symbol(cls):
        base = next(base for base in cls.__mro__ if "arg_types" in vars(base))
        return base.__module__ + "." + base.__qualname__ + ".arg_types"

    @functools.wraps(validate)
    def capture(self, *args, **kwargs):
        base = symbol(type(self))
        for key in self.arg_types:
            v = self.args.get(key)
            if v is None or (isinstance(v, list) and not v):
                missing[base + ":" + key] |= profile.active
        return validate(self, *args, **kwargs)

    Expression.error_messages = capture
    rc = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[profile])
    if rc not in (0, 1):
        raise SystemExit(rc)
    known = sum(profile.bits[n] for n in profile.passed - profile.failed)
    if not missing:
        raise ValueError("No validation events captured")
    result = {"tests": nodes, "unmeasured": hex(validator & ~known), "consumers": unknown,
              "missing": {name: hex(bits) for name, bits in missing.items()}}
    print("Validated", known.bit_count(), "tests;", len(missing), "class/key observations")
    with open(output, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps(result, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()
