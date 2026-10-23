from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


EXPECTED = "Goodbye, world!"


def load_solution_module():
    workspace = Path(os.environ["ABC_BENCH_WORKSPACE"])
    solution_path = workspace / "solution.py"
    spec = importlib.util.spec_from_file_location("abc_bench_candidate_solution", solution_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {solution_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestHelloWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solution = load_solution_module()

    def test_function_exists(self):
        self.assertTrue(hasattr(self.solution, "hello_world"))
        self.assertTrue(callable(self.solution.hello_world))

    def test_returns_exact_greeting(self):
        self.assertEqual(self.solution.hello_world(), EXPECTED)


if __name__ == "__main__":
    unittest.main()
