"""Read source-declared Python member slots on verified SQLGlot classes."""

import ast
import sys
import types

try:
    from _testcapi import type_assign_version, type_get_version
except ImportError:
    type_assign_version = type_get_version = None

MRO = type.__dict__["__mro__"]
NAMESPACE = type.__dict__["__dict__"]
QUALNAME = type.__dict__["__qualname__"]
ABSENT = object()
DEFAULT_GET = object.__getattribute__
DEFAULT_CLASS = vars(object)["__class__"]
MEMBER_READ = types.MemberDescriptorType.__get__


def slots(node):
    values = {}
    for child in node.body:
        if isinstance(child, ast.AnnAssign):
            target = child.target
        elif isinstance(child, ast.Assign) and len(child.targets) == 1:
            target = child.targets[0]
        else:
            continue
        if isinstance(target, ast.Name):
            expr = child.value
            values[target.id] = values.get(expr.id) if isinstance(expr, ast.Name) else expr
    try:
        names = ast.literal_eval(values.get("__slots__"))
    except (ValueError, TypeError):
        return ()
    if type(names) is str:
        names = (names,)
    if type(names) not in (tuple, list) or not all(type(n) is str for n in names):
        return ()
    return tuple(n for n in names if not n.startswith("__"))


def declarations(body, parents=()):
    for cls in body:
        if not isinstance(cls, ast.ClassDef) or cls.decorator_list:
            continue
        path = (*parents, cls.name)
        yield path, slots(cls)
        yield from declarations(cls.body, path)


class SlotFacts:
    def __init__(self):
        self.modules = {}
        self.names = set()
        self.classes = set()
        self.descriptors = {}
        self.cache = {}
        self.truth_cache = {}

    def default_truth(self, cls):
        ordinary = type(cls) is type
        tag = type_get_version(cls) if ordinary and type_get_version else 0
        cached = self.truth_cache.get(cls) if ordinary else None
        if tag and cached and cached[0] == tag:
            return cached[1]
        safe = True
        for base in MRO.__get__(cls):
            ns = NAMESPACE.__get__(base)
            if (any(type(k) is not str for k in ns) or "__bool__" in ns or "__len__" in ns):
                safe = False
                break
        if ordinary and type_assign_version and not tag:
            type_assign_version(cls)
            tag = type_get_version(cls)
        if tag:
            self.truth_cache[cls] = tag, safe
        return safe

    def add_module(self, name, tree):
        specs = list(declarations(tree.body))
        self.modules[name] = specs
        for _, names in specs:
            self.names.update(names)

    def register(self, module):
        if type(module) is not types.ModuleType:
            return
        module_ns = vars(module)
        if any(type(k) is not str for k in module_ns):
            return
        module_name = module_ns.get("__name__")
        if type(module_name) is not str:
            return
        actual = {}
        for path, fields in self.modules.get(module_name, ()):
            if len(path) == 1:
                ns = module_ns
            elif path[:-1] in actual:
                ns = NAMESPACE.__get__(actual[path[:-1]])
            else:
                continue
            if any(type(k) is not str for k in ns):
                continue
            cls = ns.get(path[-1])
            if type(cls) is not type:
                continue
            owned = NAMESPACE.__get__(cls)
            if any(type(k) is not str for k in owned):
                continue
            owner = owned.get("__module__")
            qual = QUALNAME.__get__(cls)
            if type(owner) is not str or type(qual) is not str or owner != module_name or qual != ".".join(path):
                continue
            actual[path] = cls
            self.classes.add(cls)
            for name in fields:
                desc = owned.get(name)
                if (type(desc) is types.MemberDescriptorType and desc.__objclass__ is cls
                        and type(desc.__name__) is str and desc.__name__ == name):
                    self.descriptors[cls, name] = desc
        self.cache.clear()

    def layout(self, cls):
        tag = type_get_version(cls) if type_get_version else 0
        cached = self.cache.get(cls)
        if tag and cached and cached[0] == tag:
            return cached[1]
        bases = MRO.__get__(cls)
        order = [(base, NAMESPACE.__get__(base)) for base in bases]
        allowed = {}
        if all(type(base) is type and all(type(k) is str for k in ns) for base, ns in order):
            def raw(name):
                return next((ns[name] for _, ns in order if name in ns), ABSENT)
            if raw("__getattribute__") is DEFAULT_GET and raw("__getattr__") is ABSENT and raw("__class__") is DEFAULT_CLASS:
                for owner, ns in reversed(order):
                    for name, descriptor in ns.items():
                        if name in self.names:
                            if self.descriptors.get((owner, name), ABSENT) is descriptor:
                                allowed[name] = descriptor
                            else:
                                allowed.pop(name, None)
        if type_assign_version and not tag:
            type_assign_version(cls)
            tag = type_get_version(cls)
        if tag:
            self.cache[cls] = tag, allowed
        return allowed

    def read(self, obj, name, unknown):
        cls = type(obj)
        if type(cls) is not type or cls not in self.classes:
            return unknown
        layout = self.layout(cls)
        member = layout.get(name)
        if member is None or sys.gettrace() is not None or sys.getprofile() is not None:
            return unknown
        if any(sys.monitoring.get_tool(i) is not None for i in range(6)):
            return unknown
        # Calling the captured C member directly prevents a concurrent class
        # change from dispatching this speculative read to a new property.
        version = 0
        if type_get_version:
            proof = self.cache.get(cls)
            if not proof or proof[1] is not layout or type_get_version(cls) != proof[0]:
                return unknown
            version = proof[0]
        try:
            value = MEMBER_READ(member, obj, cls)
        except (AttributeError, TypeError):
            return unknown  # Uninitialized field or incompatible receiver.
        if (type(obj) is not cls or (version and type_get_version(cls) != version)
                or (not version and self.layout(cls).get(name) is not member)):
            return unknown
        return value
