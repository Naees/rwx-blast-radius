"""Index lexical scopes and multi-line tokens of the unmodified base."""
import ast
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tokenize
from pathlib import Path

from generate_constants import PINNED


def blocks(body, parents=()):
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield (stmt.lineno, stmt.end_lineno, "function", parents)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            yield (stmt.lineno, stmt.end_lineno, "assignment", parents)
        elif isinstance(stmt, ast.ClassDef):
            prefix = parents + ((stmt.lineno, stmt.col_offset, stmt.name),)
            yield from blocks(stmt.body, prefix)


def main():
    root, output = [Path(a).resolve() for a in sys.argv[1:]]
    if subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PINNED:
        raise ValueError("Expected pinned base")
    if subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        raise ValueError("Dirty base checkout")
    result = {}
    for path in sorted((root / "sqlglot").rglob("*.py")):
        source = path.read_text("utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        spans = [(t.start[0], t.end[0]) for t in tokens if t.end[0] > t.start[0]]
        result[str(path.relative_to(root))] = [
            hashlib.sha256(source.encode()).hexdigest(),
            sorted(blocks(ast.parse(source).body)), spans,
        ]
    with open(output, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write(json.dumps(result, separators=(",", ":")).encode())


if __name__ == "__main__":
    main()
