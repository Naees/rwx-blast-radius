"""Derive equality facts for operands observed at an identity comparison.

Only known built-in slots and verified pinned-source method bodies qualify.
Unknown rich comparisons are never invoked.
"""

import ast
import types

try:
    from _testcapi import type_assign_version, type_get_version
except ImportError:
    type_assign_version = type_get_version = None


_SCALARS = {id(cls) for cls in (str, bytes, int, bool, float)}
_IDENTITY_SLOTS = {
    (id(cls.__eq__), id(cls.__ne__)) for cls in (object, type(None), type(Ellipsis), type(NotImplemented))
}
_BUILTIN_SLOTS = {
    (id(cls.__eq__), id(cls.__ne__))
    for cls in (str, bytes, int, float, bool, list, tuple, dict, set, frozenset, range, bytearray, complex)
}
_STR_SLOTS = (id(str.__eq__), id(str.__ne__))
_MRO = type.__dict__["__mro__"]
_CLASS_DICT = type.__dict__["__dict__"]
_EXPECT_EQ = ast.dump(ast.parse("self is other or (type(self) is type(other) and hash(self) == hash(other))", mode="eval").body)
_EXPECT_NE = ast.dump(ast.parse("not self.__eq__(other)", mode="eval").body)


def _slot(cls, name):
    # Bypass descriptors, metaclass attributes and user-defined equality.
    for base in _MRO.__get__(cls):
        ns = _CLASS_DICT.__get__(base)
        if name in ns:
            return ns[name]
    return None


def _method_line(cls, name, expected):
    methods = [method for method in cls.body if getattr(method, "name", None) == name]
    if len(methods) != 1 or not isinstance(methods[0], ast.FunctionDef):
        return None
    method = methods[0]
    args = method.args
    if (method.decorator_list or args.posonlyargs or args.kwonlyargs or args.defaults
            or args.vararg or args.kwarg or [a.arg for a in args.args] != ["self", "other"]):
        return None
    body = method.body
    if len(body) == 1 and isinstance(body[0], ast.Return) and ast.dump(body[0].value) == expected:
        return method.lineno
    return None


class IdentityFacts:
    def __init__(self):
        self.gated = {}
        self.cache = {}

    @staticmethod
    def type_gates(tree):
        gates = []
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef):
                eq = _method_line(cls, "__eq__", _EXPECT_EQ)
                ne = _method_line(cls, "__ne__", _EXPECT_NE)
                if eq and ne:
                    gates.append((cls.name, eq, ne))
        return gates

    def add_type_gates(self, module, names):
        for name, eq_line, ne_line in names:
            cls = vars(module).get(name)
            if not isinstance(cls, type):
                continue
            eq, ne = _slot(cls, "__eq__"), _slot(cls, "__ne__")
            if (type(eq) is types.FunctionType and type(ne) is types.FunctionType
                    and eq.__globals__ is vars(module) and ne.__globals__ is vars(module)
                    and eq.__code__.co_firstlineno == eq_line and ne.__code__.co_firstlineno == ne_line
                    and eq.__globals__.get("type", type) is type):
                self.gated[id(eq), id(ne)] = (eq, ne, eq.__code__, ne.__code__)
        self.cache.clear()

    def slots(self, cls):
        # CPython invalidates this tag when the class or a base changes.
        # The optional accelerator affects speed only.
        version = type_get_version(cls) if type_get_version else 0
        prior = self.cache.get(id(cls))
        if version and prior is not None and prior[0] == version:
            return prior[1]
        eq, ne = _slot(cls, "__eq__"), _slot(cls, "__ne__")
        pair = id(eq), id(ne)
        if pair in _IDENTITY_SLOTS:
            kind = "identity"
        elif pair == _STR_SLOTS:
            kind = "string"
        elif pair in _BUILTIN_SLOTS:
            kind = "builtin"
        elif pair in self.gated and _slot(cls, "__getattribute__") is object.__getattribute__:
            kind = self.gated[pair]
        else:
            kind = None
        if type_assign_version and not version:
            type_assign_version(cls)
            version = type_get_version(cls)
        if version:
            self.cache[id(cls)] = (version, kind)
        return kind

    def _kind(self, obj):
        kind = self.slots(type(obj))
        if type(kind) is tuple:
            eq, ne, eq_code, ne_code = kind
            # __ne__ calls self.__eq__. Check its real binding to account for
            # an instance attribute shadowing the class method.
            bound = object.__getattribute__(obj, "__eq__")
            if (type(bound) is types.MethodType and bound.__func__ is eq and bound.__self__ is obj
                    and eq.__code__ is eq_code and ne.__code__ is ne_code
                    and eq.__globals__.get("type", type) is type):
                return "gated"
            return None
        return kind

    def state(self, left, right):
        same = left is right
        if left is None or right is None:
            if same:
                return "1="
            other = right if left is None else left
            if self._kind(other) is not None:
                return "0!"
            return "0?"
        a, b = self._kind(left), self._kind(right)
        if a == b == "identity":
            rel = "=" if same else "!"
        elif a == b == "gated" and (same or type(left) is not type(right)):
            # The verified body returns before hashing either operand.
            rel = "=" if same else "!"
        elif a == b == "string":
            rel = "=" if str.__eq__(left, right) else "!"
        elif id(type(left)) in _SCALARS and id(type(right)) in _SCALARS:
            # Exact primitive types, with no overridable user code.
            rel = "=" if left == right else "!"
        elif (type(left) is tuple and type(right) is tuple
              and all(id(type(x)) in _SCALARS for x in left)
              and all(id(type(x)) in _SCALARS for x in right)):
            rel = "=" if left == right else "!"
        else:
            rel = "?"
        return ("1" if same else "0") + rel
