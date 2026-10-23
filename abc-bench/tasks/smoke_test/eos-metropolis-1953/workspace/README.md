# EOS Metropolis Task Workspace

This workspace contains everything you need to implement the classic hard-disk Metropolis Monte Carlo simulation.

## Files

- `equation_of_state_calculation_paper.pdf` - The original 1953 Metropolis et al. paper describing the algorithm
- `solution.py` - The Python file you need to complete
- `README.md` - This file

## Your Task

Complete the implementation in `solution.py`. The file contains five functions that need to be implemented:

1. `minimum_image_delta` - Handle periodic boundary conditions
2. `periodic_distance_squared` - Compute distances with periodic boundaries
3. `has_overlap` - Check for hard-disk overlaps
4. `attempt_move` - Perform one Metropolis move attempt
5. `run_simulation` - Run the full Monte Carlo simulation

Read the docstrings carefully - they specify the exact interface and behavior required.

You are allowed to run your own intermediate checks while solving the task. For example, you can run Python snippets, create short scratch scripts, or exercise individual functions from `solution.py`. These checks are for debugging only; the hidden verifier is not available.

## Requirements

- Use only the Python standard library, NumPy, and SciPy
- Do not modify function signatures
- Ensure deterministic behavior (use `random.Random(seed)` as specified)
- Do not mutate input arguments
- Follow the algorithm details in the docstrings exactly
- Run your own sanity checks before finishing

The task setup creates a conda environment at `abc-bench-task-env` with NumPy and SciPy installed. Use `abc-bench-task-env/bin/python` for checks that should match the verification environment.

## Verification

Your solution will be tested against a hidden test suite that checks:
- Correct periodic boundary handling
- Correct overlap detection
- Proper move acceptance/rejection
- Deterministic reproducibility
- Final simulation statistics

This prototype focuses on the Metropolis hard-disk mechanics and deterministic observables.

Good luck!
