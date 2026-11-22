"""Measure the import closure of each baseline source and test module in isolation."""

import argparse
import gzip
import importlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def observe(base, path):
    sys.path.insert(0, str(base))
    name = path[:-3].replace("/", ".").removesuffix(".__init__")
    try:
        importlib.import_module(name)
    except (Exception, SystemExit):
        return None
    result = set()
    for mod in tuple(sys.modules.values()):
        file = getattr(mod, "__file__", None)
        if isinstance(file, str) and file.startswith(str(base) + os.sep):
            result.add(str(Path(file).relative_to(base)))
    return sorted(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("tests", type=Path)
    args = parser.parse_args()
    base = args.base.resolve()
    pin = subprocess.check_output(["git", "-C", base, "rev-parse", "HEAD"], text=True).strip()
    if pin != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Unexpected SQLGlot revision")
    test_files = {r["file"] for r in json.loads(args.tests.read_bytes())["tests"]}
    paths = test_files | {str(p.relative_to(base)) for p in (base / "sqlglot").rglob("*.py")
                          if p.name != "__main__.py"}
    readers = defaultdict(list)
    unknown = []
    for path in sorted(paths):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                with os.fdopen(write_fd, "w") as stream:
                    json.dump(observe(base, path), stream)
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        with os.fdopen(read_fd) as stream:
            payload = stream.read()
        _, status = os.waitpid(pid, 0)
        dependencies = json.loads(payload) if payload and status == 0 else None
        if dependencies is None:
            unknown.append(path)
        else:
            for dep in dependencies:
                readers[dep].append(path)
    target = Path(__file__).parent / "maps/imports.json.gz"
    with target.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps({"readers": readers, "unknown": unknown}, separators=(",", ":")).encode())
    print(f"{len(readers)} dependency paths, {len(unknown)} imports unavailable")


if __name__ == "__main__":
    main()
