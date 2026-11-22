"""Execute a bounded verification probe against a private copy of the patched files."""

import importlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def overlay(base, dest, changes):
    """Mirror only directories on modified paths. Unchanged siblings are symlinks."""
    children = {}
    for path, value in changes.items():
        name, sep, tail = path.partition("/")
        if not name or name in {".", ".."}:
            raise ValueError("Invalid relative source path")
        if sep:
            children.setdefault(name, {})[tail] = value
        else:
            children[name] = value
    cache = base / "__pycache__"
    if cache.is_dir():
        (dest / "__pycache__").symlink_to(cache, target_is_directory=True)
    for entry in base.iterdir():
        name = entry.name
        if name in {"__pycache__", ".git"}:
            continue
        target = dest / name
        if name not in children:
            target.symlink_to(entry, target_is_directory=entry.is_dir())
            continue
        value = children.pop(name)
        if isinstance(value, dict):
            target.mkdir()
            overlay(entry, target, value)
        elif value is not None:
            target.write_text(value, encoding="utf-8")
            # The read-only bytecode cache can be shared for all unchanged
            # siblings. A different timestamp forces parsing of the edited file.
            stamp = int(entry.stat().st_mtime) + 3600
            os.utime(target, (stamp, stamp))
    if children:
        raise ValueError("Probe only supports edits to existing files")


_spent = 0.0


def run(base, changes, module, payload, timeout):
    """Return a probe's JSON result. An unavailable or timed-out probe yields None."""
    global _spent
    if _spent + timeout > 65:
        return None
    started = time.monotonic()
    try:
        try:
            return _run(base, changes, module, payload, timeout)
        except (ImportError, OSError, ValueError, KeyError):
            return None
    finally:
        _spent += time.monotonic() - started


_project_modules = None
_db_warm = False


def _warm(base, module, payload):
    global _project_modules, _db_warm
    if _project_modules is None:
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        importlib.import_module("tests.dialects.test_dialect")
    needs_database = module == "executor_queries" or (
        module == "query_checks" and
        any(n.startswith(("tests/test_optimizer.py", "tests/test_executor.py")) for n in payload["nodes"]))
    if not _db_warm and needs_database:
        # These imports happen before making any database connection. The
        # container uses one numerical-library thread throughout.
        importlib.import_module("pandas")
        importlib.import_module("duckdb")
        _db_warm = True
    # Fixture decoding in the parent can load another dialect between probes.
    _project_modules = {name for name in sys.modules if name.split(".", 1)[0] in {"tests", "sqlglot"}}


def _forked(base, folder, changes, module, payload, timeout):
    _warm(base, module, payload)
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(reader)
            os.setsid()
            os.chdir(folder)
            sys.dont_write_bytecode = True
            sys.path[sys.path.index(str(base))] = folder
            os.environ["PYTHONPATH"] = folder
            os.environ["SKIP_INTEGRATION"] = "0"
            with open(os.devnull, "wb") as quiet:
                os.dup2(quiet.fileno(), 1)
                os.dup2(quiet.fileno(), 2)
            modified = {p[:-3].replace("/", ".").removesuffix(".__init__") for p in changes}
            if modified.intersection(_project_modules) or any(not p.endswith(".py") for p in changes):
                # Re-import the project as a unit so references and registries
                # cannot retain an old version of the code under verification.
                for name in _project_modules:
                    sys.modules.pop(name, None)
            else:
                for name in _project_modules:
                    mod = sys.modules[name]
                    if getattr(mod, "__file__", None):
                        mod.__file__ = str(Path(folder) / Path(mod.__file__).relative_to(base))
                    if hasattr(mod, "__path__"):
                        mod.__path__ = [str(Path(folder) / Path(p).relative_to(base)) for p in mod.__path__]
            with os.fdopen(writer, "w") as output:
                json.dump(importlib.import_module(module).handle(payload), output)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(writer)
    chunks = bytearray()
    deadline = time.monotonic() + timeout
    complete = False
    try:
        while len(chunks) <= 65536:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([reader], [], [], left)[0]:
                break
            part = os.read(reader, 8192)
            if not part:
                complete = True
                break
            chunks.extend(part)
    finally:
        os.close(reader)
        if not complete:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        _, status = os.waitpid(pid, 0)
    if complete and status == 0 and chunks:
        return json.loads(chunks)
    return None


def _run(base, changes, module, payload, timeout):
    base = Path(base).resolve()
    with tempfile.TemporaryDirectory(prefix="check-") as folder:
        overlay(base, Path(folder), changes)
        if sys.platform == "linux":
            return _forked(base, folder, changes, module, payload, timeout)
        env = {**os.environ, "PYTHONPATH": folder, "PYTHONDONTWRITEBYTECODE": "1",
               "SKIP_INTEGRATION": "0", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), module],
                                cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = proc.communicate(json.dumps(payload).encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            return None
        if proc.returncode or len(out) > 65536:
            return None
        try:
            return json.loads(out)
        except (ValueError, UnicodeError):
            return None


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    with os.fdopen(os.dup(1), "w") as output, open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        result = importlib.import_module(sys.argv[1]).handle(payload)
        json.dump(result, output)
