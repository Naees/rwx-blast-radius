"""Interpreter startup hook for child processes during baseline map collection."""

import os


def trace():
    import atexit
    import json
    import sys
    from collections import defaultdict
    from source_reads import direct_reader

    prefix = os.environ["BLAST_TRACE_ROOT"] + os.sep
    mask = os.environ["BLAST_TRACE_MASK"]
    directory = os.environ["BLAST_TRACE_DIR"]
    lines = defaultdict(set)
    reads = set()
    sources = set()

    def record(code, number):
        if code.co_filename.startswith(prefix):
            lines[code.co_filename[len(prefix):]].add(number)
        return sys.monitoring.DISABLE

    def audit(event, args):
        if event == "open" and isinstance(args[0], str):
            path = os.path.abspath(args[0])
            if path.startswith(prefix) and not path.endswith(".pyc"):
                relative = path[len(prefix):]
                reads.add(relative)
                if relative.endswith(".py") and direct_reader(sys._getframe(1), lambda name: name.startswith(prefix)):
                    sources.add(relative)

    def finish():
        sys.monitoring.set_events(tool, 0)
        payload = {"mask": mask, "files": {f: sorted(n) for f, n in lines.items()}, "reads": sorted(reads),
                   "source_reads": sorted(sources)}
        with open(os.path.join(directory, f"{os.getpid()}.json"), "w") as f:
            json.dump(payload, f)

    tool = sys.monitoring.COVERAGE_ID
    sys.monitoring.use_tool_id(tool, "baseline-child-dependencies")
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, record)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    sys.addaudithook(audit)
    atexit.register(finish)


if os.environ.get("BLAST_TRACE_DIR") and os.environ.get("BLAST_TRACE_MASK", "0") != "0":
    trace()
