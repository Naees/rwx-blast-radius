"""Map helper lines executed at import to the module they initialize, on the pinned base."""

import gzip
import importlib
import inspect
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from generate_map import PINNED


def main():
    root, inventory, output = [Path(arg).resolve() for arg in sys.argv[1:]]
    revision = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if revision != PINNED or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected a clean pinned SQLGlot checkout")
    prefix = str(root) + os.sep
    calls = defaultdict(lambda: defaultdict(set))
    def trace(frame, event, _):
        name = frame.f_code.co_filename
        if event != "call" or not name.startswith(prefix) or not frame.f_code.co_flags & inspect.CO_NEWLOCALS:
            return None
        caller = frame.f_back
        while caller:
            if caller.f_code.co_name == "<module>" and caller.f_code.co_filename.startswith(prefix):
                owner = caller.f_code.co_filename[len(prefix):]
                lines = calls[name[len(prefix):]][owner]
                def local(frame, event, _):
                    if event == "line":
                        lines.add(frame.f_lineno)
                    return local
                return local
            caller = caller.f_back
        return None

    tests = {r["file"] for r in json.loads(inventory.read_bytes())["tests"]}
    src = {p.relative_to(root).as_posix() for p in (root / "sqlglot").rglob("*.py")
           if p.name != "__main__.py"}
    sys.path.insert(0, str(root))
    os.chdir(root)
    sys.settrace(trace)
    failed = []
    try:
        for path in sorted(tests | src):
            module = path[:-3].replace("/", ".").removesuffix(".__init__")
            try:
                importlib.import_module(module)
            except ImportError:
                failed.append(path)
    finally:
        sys.settrace(None)
    data = {name: {owner: sorted(lines) for owner, lines in sorted(owners.items())}
            for name, owners in sorted(calls.items())}
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps(data, separators=(",", ":")).encode())
    print(f"{len(data)} source paths. Imports unavailable: {failed}")


if __name__ == "__main__":
    main()
