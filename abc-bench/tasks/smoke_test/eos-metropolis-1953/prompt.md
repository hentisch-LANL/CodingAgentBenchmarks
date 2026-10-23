# Task: Equation of State Calculations by Fast Computing Machines

## Overview

Implement the classic hard-disk Metropolis Monte Carlo algorithm from the 1953 paper "Equation of State Calculations by Fast Computing Machines" by Metropolis, Rosenbluth, Rosenbluth, Teller, and Teller.

## Problem Description

You must implement a 2D hard-disk Monte Carlo simulation with periodic boundary conditions. Hard disks are particles that cannot overlap - they represent the limiting case of very short-range repulsive interactions.

The simulation uses the Metropolis algorithm:
1. Start with an initial configuration of non-overlapping disks
2. Randomly select a particle
3. Propose a random displacement
4. Accept the move if it doesn't cause overlap; reject otherwise
5. Repeat for many steps
6. Compute statistics about the system

## Implementation Requirements

Complete the functions in `solution.py`. The file contains detailed docstrings for each function.

### Critical Requirements

**Allowed dependencies:** You may use the Python standard library, NumPy, and SciPy. Do not use other third-party dependencies.

The task environment is a conda environment created at `abc-bench-task-env` in this workspace. If you want to run checks with the same Python environment used by verification, run commands such as `abc-bench-task-env/bin/python your_check.py`.

**No internet access.** All information needed is in `equation_of_state_calculation_paper.pdf` and this prompt.

**Complete only `solution.py`.** Do not modify function signatures or create additional files.

You may run your own code while working. In particular, you are encouraged to write small temporary checks, run Python snippets, and exercise individual functions as you implement them. These self-checks are not the hidden verifier; they are only for your own debugging. The final submitted artifact is still `solution.py`.

**Implement periodic boundary conditions correctly:**
- The simulation occurs in a square box of size `box_length × box_length`
- Particles wrap around at boundaries
- Distances are computed using the minimum image convention
- Positions should be wrapped into the range `[0, box_length)`

**Implement hard-disk constraints:**
- Particles are disks of diameter `diameter`
- Two particles overlap if their center-to-center distance is less than `diameter`
- Trial moves that would cause overlap must be rejected
- Trial moves that don't cause overlap must be accepted

**Use deterministic random numbers:**
- Initialize with `random.Random(seed)`
- Select particles using `rng.randrange(len(positions))`
- Generate displacement components using `rng.uniform(-max_displacement, max_displacement)`

**Do not mutate input arguments:**
- Functions must not modify the input `positions` or `initial_positions` lists
- Return new lists when changes are made

**Return the required statistics:**
The `run_simulation` function must return a dictionary containing:
- `attempted_moves`: Total number of Monte Carlo steps (float)
- `accepted_moves`: Number of accepted moves (float)
- `acceptance_fraction`: Accepted / attempted (float)
- `packing_fraction`: Fraction of box area covered by disks (float)
- `mean_squared_displacement`: Average squared distance particles moved from initial positions (float)
- `final_x_sum`: Sum of x-coordinates after simulation (float)
- `final_y_sum`: Sum of y-coordinates after simulation (float)

## Algorithm Details

### Periodic Boundaries

For a box of length L, the minimum image convention means:
- If two particles are separated by more than L/2 in any direction, consider the distance through the periodic boundary instead
- For a displacement δ, the minimum image displacement is: δ - L × round(δ/L)

### Move Attempt

For each Monte Carlo step:
1. Select a random particle uniformly: `particle_index = rng.randrange(len(positions))`
2. Generate a random displacement:
   - `dx = rng.uniform(-max_displacement, max_displacement)`
   - `dy = rng.uniform(-max_displacement, max_displacement)`
3. Compute candidate position: `(x + dx, y + dy)`
4. Wrap candidate into `[0, box_length)` using modulo
5. Check if candidate overlaps any other particle (excluding the particle being moved)
6. If no overlap: accept the move and update positions
7. If overlap: reject the move and leave positions unchanged

### Statistics

After all steps:
- Compute mean squared displacement by averaging the squared distance (using periodic boundaries) between each particle's final and initial position
- Compute packing fraction as: `N × π × (diameter/2)² / box_length²` where N is the number of particles
- Compute coordinate sums for verification

This first benchmark is intentionally focused on the Metropolis hard-particle mechanics and deterministic simulation observables. It is an infrastructure prototype for later, more faithful equation-of-state benchmarks that may test pressure or compressibility estimates directly.

## Reference

Read `equation_of_state_calculation_paper.pdf` for the original description of the algorithm. The paper discusses Monte Carlo methods for computing equation of state properties of molecular systems.

## Submission

Your completed `solution.py` file in this workspace will be evaluated by a hidden test suite.

Before finishing, run your own sanity checks if possible. Useful checks include:
- Testing periodic distances for particles on opposite sides of the box
- Testing accepted and rejected hard-disk moves
- Running `run_simulation` twice with the same seed and confirming identical output
- Verifying that input position lists are not mutated

The tests will check:
1. Correct implementation of periodic boundary conditions
2. Correct overlap detection (including periodic images)
3. Proper accept/reject behavior
4. Deterministic reproducibility given a seed
5. Correct computation of final statistics
6. That inputs are not mutated

Good luck!
