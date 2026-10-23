# Smoke Test Task Group — Environment

These are small sanity-check benchmarks for the ABC Bench infrastructure and
basic agent behavior. They are ordinary Python tasks; **there is no internet
access.**

## Environment

- Python 3 is available on `PATH`.
- Some tasks provide a dedicated task environment as a conda env at
  `abc-bench-task-env` inside the workspace. When it exists, run your own checks
  with the same interpreter the verifier uses, e.g.
  `abc-bench-task-env/bin/python your_check.py`.
- Allowed dependencies are stated in each task prompt (typically the Python
  standard library, and sometimes NumPy/SciPy). Do not rely on other
  third-party packages.

## Deliverable and how to check it

- Implement the file the task prompt names (commonly `solution.py`). Do not
  modify function signatures or create extra files unless the prompt allows it.
- Run your own small checks as you work — exercise individual functions, confirm
  determinism where seeds are specified, and verify inputs are not mutated.
  These self-checks are for your own debugging and are not the hidden verifier.
- Leave the completed file in place when finished.
