"""Observe which pinned tests call functions with a value matching a scalar default."""

import ast
import concurrent.futures
import gzip
import importlib.machinery
import json
import os
import subprocess
import sys
import types
from collections import defaultdict
from pathlib import Path

import pytest

from default_args import profile
from generate_map import PINNED, Recorder, TracedPool


class DefaultValues(Recorder):
    def __init__(self, root, tests, index):
        super().__init__(root, tests)
        self.slot = 3
        self.ids = {}
        self.signatures = defaultdict(list)
        self.functions = {}
        self.loaded = set()
        self.cold = set()
        self.matches = defaultdict(int)
        self.subprocesses = 0
        for path, (_, sites) in index.items():
            targets = {(row[0], row[1]) for row in sites if not row[-1]}
            for function in ast.walk(ast.parse((root / path).read_bytes())):
                if not isinstance(function, ast.FunctionDef):
                    continue
                args = function.args
                positional = [*args.posonlyargs, *args.args]
                start = len(positional) - len(args.defaults)
                pairs = [
                    *zip(positional[start:], args.defaults),
                    *zip(args.kwonlyargs, args.kw_defaults),
                ]
                for parameter, default in pairs:
                    if (
                        default is None
                        or (default.lineno, default.col_offset) not in targets
                    ):
                        continue
                    key = f"{path}:{default.lineno}:{default.col_offset}"
                    site = self.ids.setdefault(key, len(self.ids))
                    signature = (path, function.lineno, function.name)
                    self.signatures[signature].append(
                        (site, parameter.arg, ast.literal_eval(default))
                    )

    def attach(self, path, code):
        parameters = self.signatures.get((path, code.co_firstlineno, code.co_name))
        if parameters is not None:
            # Keep the code alive across reloads so Python cannot reuse its id.
            self.functions[id(code)] = (code, parameters)
            self.loaded.update(site for site, _, _ in parameters)
            sys.monitoring.set_local_events(
                self.slot, code, sys.monitoring.events.PY_START
            )
        for child in code.co_consts:
            if isinstance(child, types.CodeType):
                self.attach(path, child)

    def enter(self, code, _offset):
        _, parameters = self.functions[id(code)]
        if not self.active:
            self.cold.update(site for site, _, _ in parameters)
            return sys.monitoring.DISABLE
        frame = sys._getframe(1)
        if frame.f_code is not code:
            raise RuntimeError("Monitoring did not expose the function entry frame")
        values = frame.f_locals
        known = True
        for site, name, default in parameters:
            if self.matches[site] & self.active == self.active:
                continue
            value = values[name]
            # An omitted argument has precisely its immutable default type and
            # value. Exact types avoid invoking any user-defined comparison.
            if type(value) is type(default) and value == default:
                self.matches[site] |= self.active
            else:
                known = False
        if known:
            return sys.monitoring.DISABLE

    def audit(self, event, args):
        if self.active and event == "subprocess.Popen":
            self.subprocesses |= self.active

    def activate(self, mask):
        if self.active != mask:
            self.active = mask
            sys.monitoring.restart_events()

    def snapshot(self):
        incomplete = self.subprocesses
        for node in set(self.tests) - (self.passed - self.failed):
            incomplete |= self.bits[node]
        return {
            "tests": self.tests,
            "unmeasured": hex(incomplete),
            "masks": {
                key: hex(self.matches[site])
                for key, site in self.ids.items()
                if site in self.loaded and site not in self.cold
            },
        }


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, root, recorder):
        super().__init__(name, path)
        self.relative = Path(path).relative_to(root).as_posix()
        self.recorder = recorder

    def get_code(self, fullname):
        code = super().get_code(fullname)
        self.recorder.attach(self.relative, code)
        return code


class Finder:
    def __init__(self, root, recorder):
        self.root = root
        self.recorder = recorder

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if (
                spec
                and spec.origin
                and spec.origin.endswith(".py")
                and Path(spec.origin).is_relative_to(self.root)
            ):
                spec.loader = Loader(fullname, spec.origin, self.root, self.recorder)
            return spec
        return None


def main():
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PINNED
        or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode
    ):
        raise ValueError("Expected clean pinned checkout")
    tests = sorted(row["id"] for row in json.loads(inventory.read_bytes())["tests"])
    recorder = DefaultValues(root, tests, profile(root))
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = TracedPool
    sys.meta_path.insert(0, Finder(root, recorder))
    sys.addaudithook(recorder.audit)
    sys.monitoring.use_tool_id(recorder.slot, "default argument observer")
    try:
        sys.monitoring.register_callback(
            recorder.slot, sys.monitoring.events.PY_START, recorder.enter
        )
        rc = pytest.main(
            ["-q", "--tb=short", "-p", "no:cacheprovider", "tests"], plugins=[recorder]
        )
    finally:
        sys.monitoring.free_tool_id(recorder.slot)
    if rc not in (0, 1):
        raise SystemExit(rc)
    snapshot = recorder.snapshot()
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(snapshot, separators=(",", ":")).encode())
    print(
        f"{len(snapshot['masks'])} observed defaults; {int(snapshot['unmeasured'], 16).bit_count()} tests unmeasured"
    )


if __name__ == "__main__":
    main()
