"""Select SQL fixture consumers using the pinned loader's decoded rows and fields."""

import hashlib
import pickle
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

from mapdata import load


@lru_cache(1)
def _profile():
    return load(Path(__file__).parent / "maps/fixture_usage.json.gz")


def _rows(text):
    # These boundaries, comment rules, whitespace handling and metadata keys
    # are the ones used by tests.helpers.load_sql_fixture_pairs on the pin.
    sql = "\n".join(
        line for line in text.splitlines() if line and not line.startswith("--")
    )
    statements = sql.split(";")
    if len(statements) % 2 and statements[-1].strip():
        raise ValueError("Unpaired SQL statement")
    rows = []
    for pos in range(0, len(statements) - 1, 2):
        lines = statements[pos].strip().split("\n")
        meta = {}
        i = 0
        while i < len(lines) and lines[i].startswith("#"):
            key, value = lines[i].split(":", 1)
            meta[key.lstrip("#").strip()] = value.strip()
            i += 1
        if i == len(lines):
            raise ValueError("Metadata without SQL")
        rows.append((meta, "\n".join(lines[i:]), statements[pos + 1].strip()))
    return rows


@lru_cache(12)
def _base(path, digest):
    text = path.read_text("utf8")
    if hashlib.sha256(text.encode()).hexdigest() != digest:
        raise ValueError("Fixture does not match profile")
    return text, _rows(text)


def _sqlglot(base):
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    # The pin configures unit-test behavior on first import of its core module.
    import unittest  # noqa: F401
    import sqlglot

    return sqlglot


def _same_expression(base, before, after, dialect):
    # dump() includes comments, typed literal content, and positional metadata.
    # This stricter equivalence also covers optimizers that inspect that metadata.
    try:
        parse = _sqlglot(base).parse_one
        return parse(before, read=dialect).dump() == parse(after, read=dialect).dump()
    except Exception:
        # SQLGlot raises its own parse/dialect exceptions.
        return False


def _cache_keys_stay_distinct(uses, new_counts, before, after):
    return (
        new_counts[after] == 1
        and uses.get(hashlib.sha256(before.encode()).hexdigest()) == 1
        and hashlib.sha256(after.encode()).hexdigest() not in uses
    )


def _worker_can_skip(base, text):
    try:
        expression = _sqlglot(base).parse_one(text)
        pickle.loads(pickle.dumps(expression))
        return True
    except Exception:
        return False


def _same_duckdb(base, before, after):
    try:
        transpile = _sqlglot(base).transpile
        return (
            transpile(before, write="duckdb")[0] == transpile(after, write="duckdb")[0]
        )
    except Exception:
        return False


def select(patch, base, test_ids, deps):
    path = patch.path
    if (
        not path.startswith("tests/fixtures/")
        or not path.endswith(".sql")
        or patch.old_path != patch.new_path
        or patch.binary
    ):
        return None
    data = _profile()
    entry = data["files"].get(path)
    if entry is None:
        return None
    try:
        text, before = _base(Path(base) / path, entry["sha256"])
        replacement = patch.apply(text)
        if replacement == text:
            return set()
        after = _rows(replacement)
    except (OSError, ValueError):
        return None
    if len(before) != len(after) or len(before) != len(entry["rows"]):
        return None
    tests = data["tests"]
    pool = data["pool"]

    def bits(index):
        return int(pool[index], 16) if index is not None else 0

    selected = bits(entry["raw"])
    new_counts = Counter(row[1] for row in after)
    optimizer = bits(entry.get("optimizer"))
    # A wholesale rewrite can contain hundreds of queries. Field ownership
    # remains available when parsing the changed queries would exceed its value.
    changed_rows = sum(lhs != rhs for lhs, rhs in zip(before, after))
    parse_budget = 48_000 if changed_rows <= 8 else 0
    for i, (lhs, rhs) in enumerate(zip(before, after)):
        if lhs == rhs:
            continue
        observed = entry["rows"][i]
        old_meta, old_sql, old_expected = lhs
        meta, sql, expected = rhs
        parse_budget -= (len(sql) + len(old_sql)) * (old_sql != sql)
        parse_budget -= (len(expected) + len(old_expected)) * (old_expected != expected)
        replay = parse_budget >= 0
        if meta != old_meta:
            selected |= bits(observed.get("meta:*"))
            for key in meta.keys() | old_meta.keys():
                if meta.get(key) != old_meta.get(key):
                    selected |= bits(observed.get("meta:" + key))
        if old_expected != expected:
            selected |= bits(observed.get("expected:read")) | bits(
                observed.get("expected:parse")
            )
            duckdb = bits(observed.get("expected:duckdb"))
            if duckdb and (
                not replay or not _same_duckdb(base, old_expected, expected)
            ):
                selected |= duckdb
        if old_sql != sql:
            full = bits(observed.get("sql:read"))
            parse = bits(observed.get("sql:parse"))
            cache = bits(observed.get("sql:cache"))
            duckdb = bits(observed.get("sql:duckdb"))
            if not replay:
                selected |= full | parse | cache | duckdb
                continue
            same_dialect = meta.get("dialect") == old_meta.get("dialect")
            default_equal = None
            if parse:
                default_equal = _same_expression(base, old_sql, sql, None)
            # The pinned optimizer helper consumes the input via parse_one and
            # compares the formatted optimized tree to the expected string.
            if (
                full & optimizer
                and same_dialect
                and (
                    default_equal
                    if default_equal is not None and not meta.get("dialect")
                    else _same_expression(base, old_sql, sql, meta.get("dialect"))
                )
            ):
                full &= ~optimizer
            # The executor submits a parsed expression for every row, even those
            # whose worker returns immediately because execute metadata is absent.
            if parse and (
                default_equal
                or (
                    not meta.get("execute")
                    and not old_meta.get("execute")
                    and _worker_can_skip(base, sql)
                )
            ):
                parse = 0
            if (cache or duckdb) and _same_duckdb(base, old_sql, sql):
                duckdb = 0
                # cached_execute uses the original SQL as its key. Retain
                # cache readers if a changed key can alias a second query.
                if _cache_keys_stay_distinct(data["cache"], new_counts, old_sql, sql):
                    cache = 0
            selected |= full | parse | cache | duckdb
    hits = {n for i, n in enumerate(tests) if selected >> i & 1}
    return (
        hits
        | (set(data["unknown"]) & deps.fixture(path))
        | (set(test_ids) - set(tests))
    ) & set(test_ids)
