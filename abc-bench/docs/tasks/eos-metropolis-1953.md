# EOS Metropolis 1953

Real scientific computing benchmark: 2D hard-disk Metropolis Monte Carlo simulation.

## Overview

| Property | Value |
|----------|-------|
| **Task ID** | `eos-metropolis-1953` |
| **Task Group** | `smoke_test` |
| **Type** | Scientific computing |
| **Dependencies** | NumPy, SciPy (via `smoke_test` task-group container template) |
| **Expected Duration** | 5-30 minutes |
| **Verification** | Numerical reference comparison |

## Background

Based on the classic 1953 paper **"Equation of State Calculations by Fast Computing Machines"** by Metropolis, Rosenbluth, Rosenbluth, Teller, and Teller.

This paper introduced the **Metropolis algorithm** — the foundation of Markov Chain Monte Carlo (MCMC) methods used throughout computational physics, chemistry, and statistics.

## Task Description

Implement a **2D hard-disk Monte Carlo simulation** with periodic boundary conditions.

### Hard-Disk Model

- Particles are disks of diameter `diameter`
- Disks cannot overlap (center-to-center distance ≥ diameter)
- Represents limiting case of very short-range repulsive interactions

### Metropolis Algorithm

1. Start with non-overlapping initial configuration
2. Randomly select a particle
3. Propose random displacement
4. Accept if no overlap; reject otherwise
5. Repeat for many steps
6. Compute statistics

## Required Functions

Complete 5 functions in `solution.py`:

### 1. `minimum_image_delta(delta, box_length) -> float`

Return nearest periodic-image displacement.

```python
>>> minimum_image_delta(0.0, 10.0)
0.0
>>> minimum_image_delta(4.9, 10.0)
4.9
>>> minimum_image_delta(5.1, 10.0)
-4.9
```

### 2. `periodic_distance_squared(a, b, box_length) -> float`

Squared distance using minimum image convention.

### 3. `has_overlap(positions, particle_index, candidate, diameter, box_length) -> bool`

Check if candidate position overlaps any other particle (with periodic boundaries).

### 4. `attempt_move(positions, particle_index, displacement, diameter, box_length) -> (list, bool)`

Attempt one Metropolis move. Returns `(new_positions, accepted)`.

### 5. `run_simulation(*, initial_positions, diameter, box_length, max_displacement, seed, steps) -> dict`

Main simulation driver. Returns dict with:

- `attempted_moves`
- `accepted_moves`
- `acceptance_fraction`
- `packing_fraction`
- `mean_squared_displacement`
- `final_x_sum`
- `final_y_sum`

## Implementation Requirements

- **Allowed**: Python stdlib, NumPy, SciPy
- **Random**: Use `random.Random(seed)`, `rng.randrange()`, `rng.uniform()`
- **Immutability**: Don't modify input lists
- **Periodic boundaries**: Wrap positions to `[0, box_length)`
- **Deterministic**: Same seed = same results

## Verification

Hidden test suite (`verification/test_solution.py`) includes:

1. **Function existence** — All 5 functions present and callable
2. **Periodic boundaries** — Minimum image convention tests
3. **Distance calculations** — Periodic distance with known values
4. **Overlap detection** — Including periodic images
5. **Move accept/reject** — Input immutability, wrapping
6. **Reference simulations** — Two full simulation cases with exact expected values
7. **Input immutability** — Initial positions not mutated

### Reference Cases

The test includes two reference simulations with known outputs:

```python
# Case 1
initial_positions = [(1.0, 1.0), (3.5, 1.0), (1.0, 3.5), (3.5, 3.5), (6.0, 1.0), (6.0, 3.5)]
diameter = 0.9
box_length = 8.0
max_displacement = 0.35
seed = 1729
steps = 250

# Case 2 (12 particles)
initial_positions = [...]  # 12 particles in grid
diameter = 1.05
box_length = 6.4
max_displacement = 0.22
seed = 314159
steps = 400
```

Expected values are computed from a reference implementation in the test file.

## Files

### `prompt.md`
Complete task description with algorithm details, requirements, and reference to the 1953 paper.

### `workspace/solution.py`
Detailed stub with **NumPy-style docstrings** for all 5 functions including:
- Parameters with types
- Returns with types
- Doctest-style Examples

### `workspace/equation_of_state_calculation_paper.pdf`
Original 1953 Metropolis paper (provided for reference).

### `verification/test_solution.py`
~270 lines of tests with reference implementation.

## Running the Task

```bash
abc-bench run \
  --task smoke_test/eos-metropolis-1953 \
  --agent opencode-default \
  --timeout 3600
```

**Note**: This task often benefits from an explicit `--timeout` because
verification and agent execution can take substantially longer than small smoke
tests.

## Expected Result

**PASS** — All 7 test methods pass with numerical tolerance `1e-10` to `1e-12`

**FAIL** — Any test failure (wrong algorithm, mutability, numerical error)

## Manual Testing

```bash
cd tasks/smoke_test/eos-metropolis-1953/workspace
# Implement all 5 functions in solution.py

# Test with task conda env
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
```

### Using Task Environment

```bash
# The smoke_test container template already includes numpy/scipy
abc-bench-task-env/bin/python -c "
import solution
result = solution.run_simulation(
    initial_positions=[(1.0, 1.0), (3.0, 1.0)],
    diameter=0.8, box_length=5.0, max_displacement=0.2,
    seed=42, steps=100
)
print(result)
"
```

## Scientific Context

This task implements the core of the original 1953 simulation. The full equation of state requires:
- Running at multiple densities (packing fractions)
- Computing pressure from virial theorem
- Finite-size scaling

This benchmark focuses on **correct implementation of the Metropolis mechanics** — the foundation for more advanced physics benchmarks.

## References

- **Original paper**: Metropolis et al., *J. Chem. Phys.* **21**, 1087 (1953)
- **Paper PDF**: Provided in workspace as `equation_of_state_calculation_paper.pdf`
- **Algorithm**: Metropolis Monte Carlo with hard-disk interactions

## Purpose

This is the **first real scientific task** in ABC Bench. It tests:
- Numerical algorithm implementation
- Periodic boundary conditions
- Deterministic random number generation
- Scientific computing best practices (immutability, precision)
- Ability to work with NumPy/SciPy in isolated environment
