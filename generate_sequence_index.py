"""Record length and element identity for literal-index reads of exact lists or tuples."""
import ast
import concurrent.futures
import importlib.abc
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from generate_literal_values import Literals, Transform as Scoped
from generate_map import PINNED, TracedPool
from observe_base import ProbeFinder
from sequence_index import access, index, key


_HANDLER = ast.dump(ast.parse("try:\n    return seq[index]\nexcept IndexError:\n    return None").body[0])
_INDEX_ERROR = IndexError
try:
    from _testcapi import dict_get_version
except ImportError:
    dict_get_version = None


class Sequences(Literals):
    loading = 0
    helper = None
    code = None
    handler_state = None

    def bind(self, module, tree):
        if self.helper is not None:  # Keep a stable reference through module reloads.
            return
        definitions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "seq_get"]
        fn = vars(module).get("seq_get")
        if len(definitions) != 1 or type(fn) is not types.FunctionType:
            return
        decl = definitions[0]
        body = decl.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and type(body[0].value.value) is str:
            body = body[1:]
        if (len(body) == 1 and ast.dump(body[0]) == _HANDLER and not decl.decorator_list and not fn.__closure__
                and fn.__code__.co_firstlineno == decl.lineno and fn.__globals__ is vars(module)
                and [a.arg for a in decl.args.args] == ["seq", "index"]):
            self.helper, self.code = fn, fn.__code__

    def handler(self):
        fn = self.helper
        if fn is None or fn.__code__ is not self.code:
            return False
        g, b = fn.__globals__, fn.__builtins__
        if type(g) is not dict or type(b) is not dict:
            return False
        tags = (dict_get_version(g), dict_get_version(b)) if dict_get_version else None
        if tags is not None and self.handler_state is not None and tags == self.handler_state[0]:
            return self.handler_state[1]
        # Looking up a name in a dict with custom keys can invoke equality.
        valid = all(type(k) is str for d in (g, b) for k in d) and g.get("IndexError", b.get("IndexError")) is _INDEX_ERROR
        if tags is not None:
            self.handler_state = tags, valid
        return valid

    def probes(self, name, optional=False):
        state = self.sites.setdefault(name, {"values": {}, "opaque": 0, "startup": False})
        values = state["values"]
        def opaque():
            if not self.active or self.loading:
                state["startup"] = True
            else:
                state["opaque"] |= self.active
        def callee(fn):
            if fn is not self.helper or self.helper is None:
                opaque()
            return fn
        def record(seq):
            active = self.active
            if not active or self.loading or (type(seq) is not list and type(seq) is not tuple):
                opaque()
            elif optional and not self.handler():
                # The sequence expression runs after the callee is looked up.
                # It can modify that function or its exception binding.
                opaque()
            elif sys.gettrace() is not None or sys.getprofile() is not None:
                opaque()
            elif not state["startup"] and active & ~state["opaque"]:
                size = len(seq)
                labels = ()
                if size <= 12:
                    items = tuple(seq)
                    size = len(items)
                    if size <= 12:
                        labels = tuple(0 if v is None else
                                       next(i for i, first in enumerate(items, 1) if first is v) for v in items)
                shape = size, labels
                if shape in values or len(values) < 128:
                    values[shape] = values.get(shape, 0) | active
                else:
                    opaque()
            return seq
        return callee, record


class Transform(Scoped):
    visit_Compare = ast.NodeTransformer.generic_visit

    def wrap(self, expr, callback):
        ref = ast.Subscript(ast.Name("_br_sequence_index", ast.Load()), ast.Constant(len(self.probes)), ast.Load())
        self.probes.append(callback)
        return ast.copy_location(ast.Call(ref, [expr], []), expr)

    def transform(self, node):
        access_info = access(node) if self.local else None
        self.generic_visit(node)
        if access_info and index(access_info[2]) is not None:
            kind, _, _ = access_info
            callee, record = self.recorder.probes(self.path + ":" + key(node, kind), kind == "optional")
            if kind == "optional":
                node.func = self.wrap(node.func, callee)
                node.args[0] = self.wrap(node.args[0], record)
            else:
                node.value = self.wrap(node.value, record)
        return node

    visit_Subscript = transform
    visit_Call = transform


class Loader(importlib.abc.Loader):
    def __init__(self, origin, recorder):
        self.origin = origin
        self.recorder = recorder
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        code = Path(self.origin).read_bytes()
        tx = Transform(self.recorder, self.recorder.relative(self.origin))
        tree = tx.visit(ast.parse(code, self.origin))
        module._br_sequence_index = tx.probes
        self.recorder.loading += 1
        try:
            exec(compile(ast.fix_missing_locations(tree), self.origin, "exec"), module.__dict__)
            if module.__name__ == "sqlglot.helper":
                self.recorder.bind(module, tree)
        finally:
            self.recorder.loading -= 1


if __name__ == "__main__":
    root, inv, out = [Path(s).resolve() for s in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Unexpected baseline")
    subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"], check=True)
    nodes = sorted(row["id"] for row in json.loads(inv.read_bytes())["tests"])
    profile = Sequences(root, nodes)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    sys.addaudithook(profile.audit)
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.meta_path.insert(0, ProbeFinder(profile, Loader))
    rc = pytest.main(["-q","--tb=short","-p","no:cacheprovider","tests"], plugins=[profile])
    if rc not in (0, 1):
        raise SystemExit(rc)
    tmp = out.with_suffix(".tmp")
    profile.save(tmp)
    tmp.replace(out)
