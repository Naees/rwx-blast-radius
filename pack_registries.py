"""Combine registry profile shards generated from the same pinned checkout."""

import ast
import gzip
import json
import sys
from pathlib import Path

from configmap import module_name


def factory_names(root):
    declarations = {}
    imports = {}
    for path in sorted((root / "sqlglot").rglob("*.py")):
        mod = module_name(path.relative_to(root).as_posix())
        tree = ast.parse(path.read_bytes())
        declarations[mod] = set()
        imports[mod] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.ImportFrom) and stmt.module:
                imports[mod].update(
                    {s.asname or s.name: (stmt.module, s.name) for s in stmt.names}
                )
            if not isinstance(stmt, ast.FunctionDef) or stmt.decorator_list:
                continue
            body = [
                s
                for s in stmt.body
                if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)
            ]
            if not body or not isinstance(body[-1], ast.Return):
                continue
            target = body[-1].value
            if (
                len(body) == 1
                and isinstance(target, ast.Lambda)
                and not target.args.defaults
            ):
                declarations[mod].add(stmt.name)
            if (
                len(body) == 2
                and isinstance(body[0], ast.FunctionDef)
                and not body[0].decorator_list
                and not body[0].args.defaults
                and not any(body[0].args.kw_defaults)
                and isinstance(target, ast.Name)
                and target.id == body[0].name
            ):
                declarations[mod].add(stmt.name)
    return {
        m: sorted(
            funcs
            | {
                alias
                for alias, (pkg, n) in imports[m].items()
                if n in declarations.get(pkg, ())
            }
        )
        for m, funcs in declarations.items()
    }


def main():
    root, output, *shards = map(Path, sys.argv[1:])
    masks, names, pool_names = {}, {}, []
    unsafe, passed = set(), set()
    inventory = None
    for shard in shards:
        with gzip.open(shard, "rt") as stream:
            data = json.load(stream)
        if inventory is not None and data["tests"] != inventory:
            raise ValueError("Different test inventories")
        inventory = data["tests"]
        passed.update(data["done"])
        unsafe.update(data["unsafe"])
        for site, bits in data["masks"].items():
            masks[site] = masks.get(site, 0) | int(bits, 16)
        offset = len(pool_names)
        for mod, parts in data["names"].items():
            names.setdefault(mod, {}).update({k: v + offset for k, v in parts.items()})
        pool_names.extend(data["pool"])
    pool, files, ranges = {}, {}, {}
    for site, mask in sorted(masks.items()):
        if site in unsafe:
            continue
        path, line, col = site.rsplit(":", 2)
        files.setdefault(path, {})[line + ":" + col] = pool.setdefault(mask, len(pool))
    for path, positions in files.items():
        tree = ast.parse((root / path).read_bytes())
        covered = set()
        for n in ast.walk(tree):
            if hasattr(n, "lineno") and f"{n.lineno}:{n.col_offset}" in positions:
                covered.update(range(n.lineno, n.end_lineno + 1))
        spans = []
        for line in sorted(covered):
            if spans and line == spans[-1][1] + 1:
                spans[-1][1] = line
            else:
                spans.append([line, line])
        ranges[path] = spans
    result = {
        "files": files,
        "pool": [hex(mask) for mask in pool],
        "names": names,
        "pool_names": pool_names,
        "factories": factory_names(root),
        "ranges": ranges,
        "tests": inventory,
        "incomplete": sorted(set(inventory) - passed),
    }
    with (
        open(output, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
    ):
        gz.write(json.dumps(result, separators=(",", ":")).encode())
    print(
        sum(map(len, files.values())), "sites,", len(result["incomplete"]), "incomplete"
    )


if __name__ == "__main__":
    main()
