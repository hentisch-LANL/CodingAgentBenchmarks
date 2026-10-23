from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path


REQUIRED_KEYS = {
    "attempted_moves",
    "accepted_moves",
    "acceptance_fraction",
    "packing_fraction",
    "mean_squared_displacement",
    "final_x_sum",
    "final_y_sum",
}


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


def wrap_coordinate(value: float, box_length: float) -> float:
    return value % box_length


def reference_minimum_image_delta(delta: float, box_length: float) -> float:
    return delta - box_length * round(delta / box_length)


def reference_periodic_distance_squared(a, b, box_length: float) -> float:
    dx = reference_minimum_image_delta(a[0] - b[0], box_length)
    dy = reference_minimum_image_delta(a[1] - b[1], box_length)
    return dx * dx + dy * dy


def reference_has_overlap(positions, particle_index, candidate, diameter, box_length):
    cutoff_squared = diameter * diameter
    for index, position in enumerate(positions):
        if index == particle_index:
            continue
        if reference_periodic_distance_squared(candidate, position, box_length) < cutoff_squared:
            return True
    return False


def reference_attempt_move(positions, particle_index, displacement, diameter, box_length):
    candidate = (
        wrap_coordinate(positions[particle_index][0] + displacement[0], box_length),
        wrap_coordinate(positions[particle_index][1] + displacement[1], box_length),
    )
    if reference_has_overlap(positions, particle_index, candidate, diameter, box_length):
        return list(positions), False
    moved = list(positions)
    moved[particle_index] = candidate
    return moved, True


def reference_run_simulation(
    *,
    initial_positions,
    diameter: float,
    box_length: float,
    max_displacement: float,
    seed: int,
    steps: int,
):
    rng = random.Random(seed)
    positions = [(float(x), float(y)) for x, y in initial_positions]
    initial = list(positions)
    accepted = 0

    for _ in range(steps):
        particle_index = rng.randrange(len(positions))
        displacement = (
            rng.uniform(-max_displacement, max_displacement),
            rng.uniform(-max_displacement, max_displacement),
        )
        positions, did_accept = reference_attempt_move(
            positions, particle_index, displacement, diameter, box_length
        )
        if did_accept:
            accepted += 1

    total_displacement_squared = 0.0
    for start, end in zip(initial, positions):
        total_displacement_squared += reference_periodic_distance_squared(end, start, box_length)

    particle_area = math.pi * (diameter / 2.0) ** 2
    return {
        "attempted_moves": float(steps),
        "accepted_moves": float(accepted),
        "acceptance_fraction": accepted / steps if steps else 0.0,
        "packing_fraction": len(positions) * particle_area / (box_length * box_length),
        "mean_squared_displacement": total_displacement_squared / len(positions),
        "final_x_sum": sum(x for x, _ in positions),
        "final_y_sum": sum(y for _, y in positions),
    }


def assert_close(testcase: unittest.TestCase, actual: float, expected: float, *, tol=1e-12):
    testcase.assertTrue(
        math.isclose(float(actual), float(expected), rel_tol=tol, abs_tol=tol),
        f"expected {expected!r}, got {actual!r}",
    )


class EosMetropolisVerifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solution = load_solution_module()

    def test_required_functions_exist(self):
        for name in (
            "minimum_image_delta",
            "periodic_distance_squared",
            "has_overlap",
            "attempt_move",
            "run_simulation",
        ):
            self.assertTrue(hasattr(self.solution, name), f"missing function {name}")
            self.assertTrue(callable(getattr(self.solution, name)), f"{name} is not callable")

    def test_minimum_image_delta(self):
        cases = [
            (0.0, 10.0, 0.0),
            (4.9, 10.0, 4.9),
            (5.1, 10.0, -4.9),
            (-5.1, 10.0, 4.9),
            (12.25, 10.0, 2.25),
            (-18.75, 10.0, 1.25),
        ]
        for delta, box_length, expected in cases:
            with self.subTest(delta=delta, box_length=box_length):
                actual = self.solution.minimum_image_delta(delta, box_length)
                assert_close(self, actual, expected)

    def test_periodic_distance_squared(self):
        cases = [
            ((1.0, 1.0), (4.0, 5.0), 10.0),
            ((0.2, 5.0), (9.8, 5.0), 10.0),
            ((9.8, 9.7), (0.3, 0.2), 10.0),
            ((2.5, 8.5), (7.5, 1.5), 10.0),
        ]
        for a, b, box_length in cases:
            with self.subTest(a=a, b=b):
                expected = reference_periodic_distance_squared(a, b, box_length)
                actual = self.solution.periodic_distance_squared(a, b, box_length)
                assert_close(self, actual, expected)

    def test_overlap_detection_includes_periodic_images(self):
        positions = [(1.0, 1.0), (4.0, 4.0), (9.7, 5.0)]
        self.assertTrue(
            self.solution.has_overlap(positions, 1, (1.75, 1.0), 1.0, 10.0),
            "candidate closer than one diameter should overlap",
        )
        self.assertFalse(
            self.solution.has_overlap(positions, 1, (2.01, 1.0), 1.0, 10.0),
            "candidate farther than one diameter should not overlap",
        )
        self.assertTrue(
            self.solution.has_overlap(positions, 1, (0.2, 5.0), 1.0, 10.0),
            "periodic-image overlap was not detected",
        )
        self.assertFalse(
            self.solution.has_overlap(positions, 0, positions[0], 1.0, 10.0),
            "a particle must not be considered overlapping with itself",
        )

    def test_attempt_move_accepts_rejects_and_preserves_input(self):
        positions = [(1.0, 1.0), (3.0, 1.0), (8.0, 8.0)]
        original = list(positions)

        moved, accepted = self.solution.attempt_move(positions, 0, (0.5, 0.25), 1.0, 10.0)
        self.assertTrue(accepted)
        self.assertEqual(positions, original, "attempt_move must not mutate its input positions")
        self.assertEqual(moved[1:], original[1:])
        assert_close(self, moved[0][0], 1.5)
        assert_close(self, moved[0][1], 1.25)

        rejected, accepted = self.solution.attempt_move(positions, 0, (1.2, 0.0), 1.0, 10.0)
        self.assertFalse(accepted)
        self.assertEqual(rejected, original, "rejected moves must leave the configuration unchanged")

        wrapped, accepted = self.solution.attempt_move(positions, 2, (4.0, 3.5), 1.0, 10.0)
        self.assertTrue(accepted)
        assert_close(self, wrapped[2][0], 2.0)
        assert_close(self, wrapped[2][1], 1.5)

    def test_run_simulation_reference_cases(self):
        cases = [
            {
                "initial_positions": [
                    (1.0, 1.0),
                    (3.5, 1.0),
                    (1.0, 3.5),
                    (3.5, 3.5),
                    (6.0, 1.0),
                    (6.0, 3.5),
                ],
                "diameter": 0.9,
                "box_length": 8.0,
                "max_displacement": 0.35,
                "seed": 1729,
                "steps": 250,
            },
            {
                "initial_positions": [
                    (0.8, 0.8),
                    (2.4, 0.8),
                    (4.0, 0.8),
                    (5.6, 0.8),
                    (0.8, 2.4),
                    (2.4, 2.4),
                    (4.0, 2.4),
                    (5.6, 2.4),
                    (0.8, 4.0),
                    (2.4, 4.0),
                    (4.0, 4.0),
                    (5.6, 4.0),
                ],
                "diameter": 1.05,
                "box_length": 6.4,
                "max_displacement": 0.22,
                "seed": 314159,
                "steps": 400,
            },
        ]

        for case in cases:
            with self.subTest(seed=case["seed"], steps=case["steps"]):
                result = self.solution.run_simulation(**case)
                self.assertIsInstance(result, Mapping, "run_simulation must return a mapping/dict")
                missing = REQUIRED_KEYS.difference(result)
                self.assertFalse(missing, f"run_simulation result is missing keys: {sorted(missing)}")
                expected = reference_run_simulation(**case)
                for key, expected_value in expected.items():
                    assert_close(self, result[key], expected_value, tol=1e-10)

    def test_run_simulation_does_not_mutate_initial_positions(self):
        initial_positions = [(1.0, 1.0), (3.0, 1.0), (1.0, 3.0), (3.0, 3.0)]
        original = list(initial_positions)
        result = self.solution.run_simulation(
            initial_positions=initial_positions,
            diameter=0.8,
            box_length=5.0,
            max_displacement=0.2,
            seed=99,
            steps=25,
        )
        self.assertEqual(initial_positions, original, "run_simulation must not mutate initial_positions")
        self.assertIsInstance(result, Mapping)


if __name__ == "__main__":
    unittest.main(verbosity=2)
