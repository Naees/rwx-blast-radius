"""Apply a Git patch and map Python syntax changes to baseline execution lines."""

import ast
import dis
import hashlib
import marshal
import re
import shlex
import types
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache

from mapdata import load as load_map


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: list = field(default_factory=list)


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)
    binary: bool = False

    @property
    def path(self):
        return self.new_path or self.old_path

    @property
    def removed_lines(self):
        return {i for i, _, kind in self._positions() if kind == "-"}

    @property
    def added_lines(self):
        return {i for _, i, kind in self._positions() if kind == "+"}

    @property
    def insertions(self):
        return {i - 1 for i, _, kind in self._positions() if kind == "+"}

    def _positions(self):
        for h in self.hunks:
            old = h.old_start if h.old_count else h.old_start + 1
            new = h.new_start if h.new_count else h.new_start + 1
            for line in h.body:
                kind = line[0]
                yield old, new, kind
                old += kind != "+"
                new += kind != "-"

    def apply(self, source):
        baseline = source.splitlines(keepends=True)
        result = []
        cursor = 0
        for h in self.hunks:
            start = h.old_start - 1 if h.old_count else h.old_start
            if not cursor <= start <= len(baseline):
                raise ValueError(f"Invalid hunk offset in {self.path}")
            result.extend(baseline[cursor:start])
            cursor = start
            consumed = written = 0
            for line in h.body:
                kind, value = line[0], line[1:]
                if kind != "+":
                    if cursor >= len(baseline) or baseline[cursor] != value:
                        raise ValueError(f"Patch context mismatch in {self.path}:{cursor+1}")
                    cursor += 1
                    consumed += 1
                if kind != "-":
                    result.append(value)
                    written += 1
            if (consumed, written) != (h.old_count, h.new_count):
                raise ValueError(f"Truncated patch in {self.path}")
        result.extend(baseline[cursor:])
        return "".join(result)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _path(header):
    raw = header.rstrip("\r\n").split("\t", 1)[0]
    if raw.startswith('"'):
        raw = ast.literal_eval(raw)
    if raw == "/dev/null":
        return None
    return raw[2:] if raw.startswith(("a/", "b/")) else raw


def parse_patch(text):
    result = []
    current = hunk = None
    left = right = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("\\ No newline at end of file") and hunk and hunk.body:
            hunk.body[-1] = hunk.body[-1].removesuffix("\n")
        elif hunk and (left or right):
            if line[:1] not in (" ", "-", "+"):
                raise ValueError("Invalid diff hunk")
            hunk.body.append(line)
            left -= line[0] != "+"
            right -= line[0] != "-"
            if left < 0 or right < 0:
                raise ValueError("Invalid diff hunk lengths")
        elif line.startswith("diff --git "):
            paths = shlex.split(line)
            current = FilePatch(_path(paths[2]), _path(paths[3]))
            result.append(current)
            hunk = None
        elif line.startswith("--- "):
            if current is None or hunk:
                current = FilePatch(None, None)
                result.append(current)
            hunk = None
            current.old_path = _path(line[4:])
        elif line.startswith("+++ ") and current:
            current.new_path = _path(line[4:])
        elif line.startswith(("Binary files ", "GIT binary patch")) and current:
            current.binary = True
        elif line.startswith("@@ ") and current:
            match = _HUNK.match(line)
            if match is None:
                raise ValueError("Invalid hunk header")
            numbers = [int(x) if x is not None else 1 for x in match.groups()]
            hunk = Hunk(*numbers)
            current.hunks.append(hunk)
            left, right = hunk.old_count, hunk.new_count
    if left or right:
        raise ValueError("Truncated diff hunk")
    return result


@dataclass
class Impact:
    lines: set = field(default_factory=set)
    changed_names: set = field(default_factory=set)
    keys: set = field(default_factory=set)
    classes: tuple = ()
    global_change: bool = False
    invalid: bool = False
    equivalent: bool = False
    expressions: list = field(default_factory=list)


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _equal(a, b):
    if isinstance(a, ast.AST) and isinstance(b, ast.AST):
        if hasattr(ast, "compare"):
            return ast.compare(a, b)
        return ast.dump(a, annotate_fields=False) == ast.dump(b, annotate_fields=False)
    return type(a) == type(b) and a == b


def _changes(old, new, trail):
    """Yield old AST ancestry at each difference, without source position noise."""
    if type(old) is not type(new):
        yield trail
    elif isinstance(old, ast.AST):
        for name in old._fields:
            a, b = getattr(old, name), getattr(new, name)
            yield from _changes(a, b, trail + [(old, new, name)])
    elif isinstance(old, list):
        if len(old) == len(new):
            for a, b in zip(old, new):
                yield from _changes(a, b, trail)
        else:
            # Preserve the unchanged prefix and suffix when statements are inserted.
            i, j = 0, 0
            while i < min(len(old), len(new)) and _equal(old[i], new[i]):
                i += 1
            while j < min(len(old), len(new)) - i and _equal(old[-1-j], new[-1-j]):
                j += 1
            for item in old[i:len(old)-j]:
                yield trail + [(item, None, None)] if isinstance(item, ast.AST) else trail
            if len(new) - j > i:
                yield trail
    elif type(old) is not type(new) or old != new:
        yield trail


def _bound_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    return {a.id for a in ast.walk(node) if isinstance(a, ast.Name) and isinstance(a.ctx, ast.Store)}


def _key_name(key):
    if isinstance(key, ast.Constant) and isinstance(key.value, (str, int)):
        return key.value
    if isinstance(key, (ast.Name, ast.Attribute)):
        return ast.unparse(key)
    return None


def _executable_lines(code):
    result = {line for _, line in dis.findlinestarts(code) if line is not None}
    for item in code.co_consts:
        if isinstance(item, types.CodeType):
            result.update(_executable_lines(item))
    return result


@lru_cache(maxsize=12)
def _baseline(source):
    tree = ast.parse(source)
    code = compile(tree, "<source>", "exec")
    return tree, _executable_lines(code), code


def _normalized(code):
    constants = tuple(_normalized(v) if isinstance(v, types.CodeType) else v for v in code.co_consts)
    return code.replace(co_consts=constants, co_firstlineno=1, co_linetable=b"")


@lru_cache(1)
def _scopes():
    try:
        return load_map(Path(__file__).parent / "maps/scopes.json.gz")
    except FileNotFoundError:
        return {}


@lru_cache(32)
def _scope_index(path, source):
    site = _scopes().get(path)
    return site if site and hashlib.sha256(source.encode()).hexdigest() == site[0] else None


def _comment_edit(patch, source, site):
    if not patch.hunks or patch.old_path != patch.new_path:
        return False
    lines = source.splitlines()
    for h in patch.hunks:
        old = h.old_start if h.old_count else h.old_start + 1
        for raw in h.body:
            kind = raw[0]
            if kind != " ":
                text = raw[1:].strip()
                if text and not text.startswith("#"):
                    return False
                # Comments inside strings are data. A physical line after a
                # backslash can also change syntax, even when it looks empty.
                if old <= 2 or (old > 1 and lines[old-2].rstrip().endswith("\\")):
                    return False
                if any(lo <= old <= hi for lo, hi in site[2]):
                    return False
            old += kind != "+"
    return True


def source_window(patch, old_source, new_source):
    site = _scope_index(patch.path, old_source)
    if not site or patch.old_path != patch.new_path:
        return old_source, new_source
    removed, added = patch.removed_lines, patch.added_lines
    if not removed or removed != added:
        return old_source, new_source
    old, new = old_source.splitlines(keepends=True), new_source.splitlines(keepends=True)
    for number in removed:
        a, b = old[number-1], new[number-1]
        if not a.strip() or not b.strip() or len(a)-len(a.lstrip()) != len(b)-len(b.lstrip()):
            return old_source, new_source
    low, high = min(removed), max(removed)
    for start, end, kind, parents in site[1]:
        if start > low:
            break
        if high > end or (start == low and kind == "function"):
            continue
        prefix = ["\n"] * (start-1)
        for line, indent, name in parents:
            prefix[line-1] = " " * indent + "class " + name + ":\n"
        prefix = "".join(prefix)
        return prefix + "".join(old[start-1:end]), prefix + "".join(new[start-1:end])
    return old_source, new_source


def analyze_python(patch, base_source, fast=True):
    result = Impact()
    new_source = patch.apply(base_source)
    if fast:
        site = _scope_index(patch.path, base_source)
        if site and _comment_edit(patch, base_source, site):
            result.equivalent = True
            return result
        old_window, new_window = source_window(patch, base_source, new_source)
        if old_window != base_source:
            try:
                return _analyze(patch, old_window, new_window)
            except (SyntaxError, ValueError):
                pass  # A scope may rely on a surrounding nonlocal declaration.
    try:
        return _analyze(patch, base_source, new_source)
    except (SyntaxError, ValueError):
        result.invalid = result.global_change = True
        return result


def _analyze(patch, base_source, new_source):
    result = Impact()
    old_tree, executable, old_code = _baseline(base_source)
    new_tree = ast.parse(new_source)
    # ast.parse accepts some compilation errors such as "break" outside a loop.
    new_code = compile(new_tree, "<source>", "exec")
    if (patch.old_path == patch.new_path and len(base_source) < 16000
            and old_code.co_code == new_code.co_code
            and marshal.dumps(_normalized(old_code), 2) == marshal.dumps(_normalized(new_code), 2)):
        # Marshalling retains signed zeros, constant types, closure layout,
        # exception tables, annotations and docstrings, among other metadata.
        return Impact(equivalent=True)
    if patch.old_path is None or patch.new_path is None or patch.old_path != patch.new_path:
        result.global_change = True
    removed = patch.removed_lines
    classes = []
    changed = False
    all_expressions = True
    pairs = {}
    for trail in _changes(old_tree, new_tree, []):
        changed = True
        result.lines.update(removed & executable)
        expr = next(((a, b) for a, b, _ in reversed(trail)
                     if isinstance(a, (ast.Compare, ast.BoolOp)) and isinstance(b, type(a))), None)
        if expr is None:
            all_expressions = False
        else:
            pairs[expr[0]] = expr
        ancestors = [(n, field) for n, _, field in trail if hasattr(n, "lineno")]
        runtime = [(n, f) for n, f in ancestors if isinstance(n, _FUNCTIONS) and f == "body"]
        if runtime:
            fn = runtime[-1][0]
            if not isinstance(fn, ast.Lambda) and fn.body and isinstance(fn.body[0], ast.Expr):
                doc = fn.body[0]
                if isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str):
                    if any(n is doc for n, _ in ancestors):
                        # A docstring is published as __doc__ at definition time.
                        result.global_change = True
                        result.changed_names.add(fn.name)
        # The innermost expression or statement provides a bytecode line anchor
        # for edits to a continuation line (and for insertion-only mutations).
        if ancestors:
            node = ancestors[-1][0]
            if node.lineno in executable:
                result.lines.add(node.lineno)
            if not runtime:
                result.global_change = True
                statements = [n for n, _ in ancestors if isinstance(n, ast.stmt)]
                if statements:
                    result.changed_names.update(_bound_names(statements[-1]))
                for n, _ in ancestors:
                    if isinstance(n, ast.ClassDef) and n.name not in classes:
                        classes.append(n.name)
                    if isinstance(n, ast.Dict):
                        relevant = removed or patch.insertions
                        for key, val in zip(n.keys, n.values):
                            if key and any(key.lineno <= ln <= val.end_lineno for ln in relevant):
                                name = _key_name(key)
                                if name is not None:
                                    result.keys.add(name)
            if runtime and (node in (x for x, _ in runtime) or not result.lines):
                # A new statement can change every call of the containing function.
                fn = runtime[-1][0]
                result.lines.update(executable.intersection(range(fn.lineno, fn.end_lineno + 1)))
        else:
            result.global_change = True
    result.expressions = list(pairs.values()) if all_expressions else []
    result.classes = tuple(classes)
    result.equivalent = not changed and not result.global_change
    return result
