"""Profile the original SQLGlot stress tests by parse phase and dialect."""

import gzip
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch


def main():
    root, output = [Path(p).resolve() for p in sys.argv[1:]]
    pin = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != pin:
        raise ValueError("Unexpected SQLGlot revision")
    if subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Expected a clean pinned checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    from tests import test_generator, test_parser
    mon = sys.monitoring
    prefix = str(root) + os.sep
    by_phase = defaultdict(lambda: defaultdict(set))
    phase = ""

    def change(name):
        nonlocal phase
        phase = name
        mon.restart_events()

    def line(code, number):
        filename = code.co_filename
        if phase and filename.startswith(prefix + "sqlglot/"):
            by_phase[phase][filename[len(prefix):]].add(number)
        return mon.DISABLE

    from stress import evidence, repeat_sql
    heavy = None
    original = test_parser.parse_one
    def parse(sql, *args, **kwargs):
        nonlocal heavy
        if len(sql) > 100_000:
            heavy = sql
        change("parse:stress" if len(sql) > 100_000 else "parse:small")
        try:
            return original(sql, *args, **kwargs)
        finally:
            change("")

    dialects = list(test_generator.Dialects)
    def selected():
        for dialect in dialects:
            change("generator:" + dialect.value)
            yield dialect
        change("")

    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "stress-subtest-map")
    mon.register_callback(tool, mon.events.LINE, line)
    mon.set_events(tool, mon.events.LINE)
    # Imports cached by an earlier dialect would otherwise be attributed
    # only to that first dialect. Also run each subtest with fresh import state.
    for dialect in dialects:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                name = "generator:" + dialect.value
                by_phase.clear()
                change(name)
                with patch.object(test_generator, "Dialects", [dialect]):
                    test_generator.TestGenerator("test_generator_memory").test_generator_memory()
                change("")
                with os.fdopen(write_fd, "w") as f:
                    json.dump({p: sorted(v) for p, v in by_phase[name].items()}, f)
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        with os.fdopen(read_fd) as f:
            raw = f.read()
        _, status = os.waitpid(pid, 0)
        if status:
            raise RuntimeError(f"Failed to profile {dialect}")
        for path, numbers in json.loads(raw).items():
            by_phase["generator:" + dialect.value][path].update(numbers)
    with patch.object(test_parser, "parse_one", parse):
        test_parser.TestParser("test_max_nodes").test_max_nodes()
    with patch.object(test_generator, "Dialects", selected()):
        test_generator.TestGenerator("test_generator_memory").test_generator_memory()
    mon.set_events(tool, 0)
    mon.free_tool_id(tool)
    phases = sorted(by_phase)
    files = defaultdict(dict)
    for i, name in enumerate(phases):
        for path, numbers in by_phase[name].items():
            for n in numbers:
                files[path][n] = files[path].get(n, 0) | 1 << i
    data = {"phases": phases, "files": files,
            "evidence": [evidence(repeat_sql(heavy, k)) for k in (2, 3, 8)]}
    with output.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps(data, separators=(",", ":")).encode())
    print(f"{len(phases)} phases, {len(files)} source files")


if __name__ == "__main__":
    main()
