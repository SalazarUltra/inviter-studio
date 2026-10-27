"""Concurrent warm-up run registry."""

import unittest
from unittest.mock import MagicMock

from core.warmup_runs import WarmupRunRegistry


class WarmupRunRegistryTests(unittest.TestCase):
    def test_put_is_warming_and_discard(self):
        registry = WarmupRunRegistry()
        future = MagicMock()
        future.done.return_value = False
        registry.put(7, engine=object(), future=future, phone="+100", label="Ann")
        self.assertTrue(registry.is_warming(7))
        self.assertTrue(registry.any_warming())
        self.assertEqual(registry.active_ids(), [7])
        self.assertEqual(registry.get(7)["label"], "Ann")
        future.done.return_value = True
        self.assertFalse(registry.is_warming(7))
        registry.prune()
        self.assertEqual(registry.active_ids(), [])

    def test_second_account_independent(self):
        registry = WarmupRunRegistry()
        a = MagicMock(done=MagicMock(return_value=False))
        b = MagicMock(done=MagicMock(return_value=False))
        registry.put(1, engine="e1", future=a, phone="+1", label="A")
        registry.put(2, engine="e2", future=b, phone="+2", label="B")
        self.assertTrue(registry.is_warming(1))
        self.assertTrue(registry.is_warming(2))
        a.done.return_value = True
        registry.discard(1)
        self.assertFalse(registry.is_warming(1))
        self.assertTrue(registry.is_warming(2))
        self.assertEqual([item[0] for item in registry.items()], [2])

    def test_engines_and_futures_lists(self):
        registry = WarmupRunRegistry()
        future = MagicMock(done=MagicMock(return_value=False))
        engine = object()
        registry.put(3, engine=engine, future=future)
        self.assertEqual(registry.engines(), [engine])
        self.assertEqual(registry.futures(), [future])


if __name__ == "__main__":
    unittest.main()
