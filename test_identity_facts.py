"""Checks for the pinned-source comparison proof used by the baseline profiler."""

import ast
import types
import unittest

from identity_facts import IdentityFacts


SOURCE = """
class Expression:
    def __eq__(self, other: object) -> bool:
        return self is other or (type(self) is type(other) and hash(self) == hash(other))
    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)
    def __hash__(self):
        raise AssertionError("The profiler must not invoke user hashing")
"""


class TestIdentityFacts(unittest.TestCase):
    def setUp(self):
        module = types.ModuleType("example")
        exec(compile(SOURCE, "example.py", "exec"), vars(module))
        self.facts = IdentityFacts()
        self.facts.add_type_gates(module, self.facts.type_gates(ast.parse(SOURCE)))
        self.expression = module.Expression

    def test_type_gate(self):
        a, b = self.expression(), self.expression()
        child = type("Child", (self.expression,), {})()
        self.assertEqual(self.facts.state(a, None), "0!")
        self.assertEqual(self.facts.state(None, a), "0!")
        self.assertEqual(self.facts.state(a, a), "1=")
        self.assertEqual(self.facts.state(a, child), "0!")
        self.assertEqual(self.facts.state(a, b), "0?")

    def test_independent_inequality_and_instance_shadowing(self):
        child = type("Child", (self.expression,), {"__ne__": lambda self, other: True})()
        self.assertEqual(self.facts.state(child, None), "0?")
        shadowed = self.expression()
        shadowed.__eq__ = lambda other: True
        self.assertEqual(self.facts.state(shadowed, None), "0?")

    def test_replaced_method_body_loses_proof(self):
        item = self.expression()
        self.assertEqual(self.facts.state(item, None), "0!")
        def different(self, other):
            return True
        self.expression.__eq__.__code__ = different.__code__
        self.assertEqual(self.facts.state(item, None), "0?")

    def test_changed_slot_invalidates_the_cache(self):
        class Plain:
            pass
        item = Plain()
        self.assertEqual(self.facts.state(item, None), "0!")
        def forbidden(self, other):
            raise AssertionError("The profiler must not invoke user equality")
        Plain.__eq__ = forbidden
        self.assertEqual(self.facts.state(item, None), "0?")


if __name__ == "__main__":
    unittest.main()
