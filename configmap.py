"""Resolve changes to class configuration using dependencies recorded at the base."""
import ast
from functools import lru_cache
from pathlib import Path

from mapdata import load as load_map

from impact import source_window


def module_name(path):
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def members(tree, module):
    """Class-body assignment sites; lexical names match runtime __qualname__."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            name = module + "." + node.name
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    target = stmt.target
                elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                else:
                    continue
                if isinstance(target, ast.Name) and stmt.value is not None:
                    yield name + "." + target.id, stmt
            yield from members(node, name)


def literal(node):
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return ...


def same_shape(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(same_shape(x, y) for x, y in zip(a, b))
    return type(a) in (int, float, bool, str, bytes, type(None))


def call_shape(node):
    if not isinstance(node, ast.Call):
        return None
    arguments = tuple(literal(a) for a in node.args)
    keywords = tuple((a.arg, literal(a.value)) for a in node.keywords)
    if ... in arguments or any(k is None or v is ... for k, v in keywords):
        return None
    return ast.dump(node.func), arguments, keywords


def _tokens(symbol, old, new):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        if same_shape(literal(old), literal(new)):
            return [symbol]
        return None

    # **base expansions must be unchanged, and the order and set of keys
    # must match. Tracking value reads cannot resolve a changed key.
    if len(old.keys) != len(new.keys):
        return None
    tokens = []
    for k, n, a, b in zip(old.keys, new.keys, old.values, new.values):
        if (k is None) != (n is None) or (k is not None and ast.dump(k) != ast.dump(n)):
            return None
        if ast.dump(a) == ast.dump(b):
            continue
        if not isinstance(k, ast.Constant) or type(k.value) is not str:
            return None
        if symbol.endswith(".arg_types") and type(literal(a)) is bool and type(literal(b)) is bool:
            tokens.append(symbol + ":" + k.value)
        elif same_shape(literal(a), literal(b)):
            tokens.append(symbol + ":" + k.value)
        else:
            lhs, rhs = call_shape(a), call_shape(b)
            if lhs is None or rhs is None or lhs[0] != rhs[0]:
                return None
            if [k for k, _ in lhs[2]] != [k for k, _ in rhs[2]]:
                return None
            if not same_shape(lhs[1:], rhs[1:]):
                return None
            tokens.append(symbol + ":" + k.value)
    return tokens


@lru_cache(1)
def _map():
    return load_map(Path(__file__).parent / "maps/config.json.gz")


@lru_cache(1)
def _arguments():
    path = Path(__file__).parent / "maps/arguments.json.gz"
    if path.is_file():
        return load_map(path)
    return None


def _argument_mask(data, symbol, token, incomplete, baseline):
    coarse = int(data["masks"][symbol], 16)
    coarse |= sum(1 << i for i, name in enumerate(data["tests"]) if name in incomplete)
    profile = _arguments()
    if not profile or profile["tests"] != data["tests"]:
        return coarse
    owner = symbol.rsplit(".", 2)[-2]
    sensitive = int(profile["unmeasured"], 16)
    for consumer in ("*", owner):
        sensitive |= int(profile["consumers"].get(consumer, "0"), 16)
    observed = int(profile["missing"].get(token, "0"), 16)
    # A change to a required-key set can reorder existing error messages.
    # Only two or more missing required keys can expose that reordering.
    prior_missing = 0
    for key, value in zip(baseline.keys, baseline.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return coarse | observed
        if not isinstance(value, ast.Constant) or type(value.value) is not bool:
            return coarse | observed
        if value.value:
            missing = int(profile["missing"].get(symbol + ":" + key.value, "0"), 16)
            observed |= missing & prior_missing
            prior_missing |= missing
    return (coarse & sensitive) | observed


def select(patch, impact, base_source, deps):
    source, changed = source_window(patch, base_source, patch.apply(base_source))
    old, new = ast.parse(source), ast.parse(changed)
    module = module_name(patch.path)
    defs = dict([*members(old, module), *module_members(old, module)])
    updates = dict([*members(new, module), *module_members(new, module)])
    if defs.keys() != updates.keys():
        return None
    data = _map()
    mask = 0
    incomplete = set(data.get("incomplete", ())).intersection(deps.file(patch.path))
    extra = set()
    for name, prior in defs.items():
        after = updates[name]
        if not (patch.removed_lines & set(range(prior.lineno, prior.end_lineno + 1))
                or patch.added_lines & set(range(after.lineno, after.end_lineno + 1))):
            continue
        tokens = _tokens(name, prior.value, after.value)
        if not tokens:
            continue
        for token in tokens:
            if name.endswith(".arg_types") and token.startswith(name + ":"):
                if name not in data["masks"]:
                    return None
                mask |= _argument_mask(data, name, token, incomplete, prior.value)
                continue
            extra.update(incomplete)
            dynamic = token in data["masks"]
            reads = _constant_hits(token, dynamic, deps)
            if reads is None:
                return None
            extra.update(reads)
            if dynamic:
                mask |= int(data["masks"][token], 16)
        # Every other syntax difference must vanish after masking profiled sites.
        after.value = prior.value
    if ast.dump(old) != ast.dump(new):
        return None
    return extra | {node for i, node in enumerate(data["tests"]) if mask >> i & 1}


def module_members(tree, module):
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        else:
            continue
        if isinstance(target, ast.Name) and target.id.isupper() and stmt.value is not None:
            yield module + "." + target.id, stmt


@lru_cache(1)
def _constants():
    return load_map(Path(__file__).parent / "maps/constants.json.gz")


def _constant_hits(symbol, dynamic, deps):
    table = _constants()
    if symbol not in table["symbols"]:
        return set() if dynamic else None
    name = symbol.split(".")[-1]
    sites = table["bare"].get(name, [])
    if not dynamic:
        sites = sites + table["qualified"].get(name, [])
    hits = set()
    for site in sites:
        if site is None:
            return None
        path, lo, hi = site
        for line in range(lo, hi + 1):
            hits.update(deps.line(path, line))
    return hits
