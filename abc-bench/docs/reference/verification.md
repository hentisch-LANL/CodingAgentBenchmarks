# Verification

Reference for the verification side of a benchmark run.

## Overview

Verification runs after the agent completes. The task's hidden verification files
are injected into the run directory only at that point, and the runner then uses
them to decide whether the benchmark passed or failed.

## Verification Flow

```
1. Agent finishes editing solution.py
2. Runner injects verification/ directory into result directory
3. Runner executes verify_solution.sh in container
4. verify_solution.sh runs test_solution.py
5. Output parsed for PASS/FAIL
```

## Verification Directory Structure

Injected at runtime:
```
results/.../verification/
├── verify_solution.sh    # Entrypoint (must print PASS/FAIL)
└── test_solution.py      # unittest test suite
```

## verify_solution.sh Specification

### Requirements

- **Executable**: `chmod +x verify_solution.sh`
- **Last line**: Must be exactly `PASS` or `FAIL`
- **Exit code**: 0 for PASS, non-zero for FAIL
- **Environment**: Receives `ABC_BENCH_WORKSPACE` and `ABC_BENCH_TASK_ENV`

### Standard Template

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${ABC_BENCH_WORKSPACE}"

# Verify solution.py exists
[[ -f "${WORKSPACE}/solution.py" ]] || { echo "FAIL"; exit 1; }

export ABC_BENCH_WORKSPACE="${WORKSPACE}"

# Use task-specific conda env if it exists
TASK_ENV="${ABC_BENCH_TASK_ENV:-${WORKSPACE}/abc-bench-task-env}"
if [[ -x "${TASK_ENV}/bin/python" ]]; then
    PYTHON="${TASK_ENV}/bin/python"
else
    PYTHON="python"
fi

# Run tests
if "${PYTHON}" "${SCRIPT_DIR}/test_solution.py"; then
    echo "PASS"
else
    echo "FAIL"
    exit 1
fi
```

### Key Points

1. **Checks solution.py exists** — Fails fast if missing
2. **Uses task conda env** — Prefers `ABC_BENCH_TASK_ENV` → `workspace/abc-bench-task-env` → system python
3. **Runs test_solution.py** — Delegates to Python unittest
4. **Prints PASS/FAIL** — Exact match required on last line
5. **Exit code matches** — 0 for PASS, 1 for FAIL

## test_solution.py Specification

### Requirements

- Uses `unittest` framework
- Loads candidate's `solution.py` dynamically from `ABC_BENCH_WORKSPACE`
- Tests are **hidden** from agent (injected after agent finishes)
- Should include reference implementation for comparison

### Standard Template

```python
"""
Hidden tests for {task-name}.
"""

import importlib.util
import os
import unittest
from pathlib import Path


def load_solution_module():
    """Load the candidate's solution.py from the workspace."""
    workspace = Path(os.environ["ABC_BENCH_WORKSPACE"])
    spec = importlib.util.spec_from_file_location(
        "abc_bench_candidate_solution",
        workspace / "solution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solution = load_solution_module()

    def test_function_name_basic(self):
        """Test basic functionality."""
        result = self.solution.function_name(...)
        self.assertEqual(result, expected)

    def test_function_name_edge_case(self):
        """Test edge cases."""
        ...

    def test_function_name_reference(self):
        """Test against known reference values."""
        ...


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

### Key Components

#### `load_solution_module()`

Dynamically imports the candidate's solution:

```python
def load_solution_module():
    workspace = Path(os.environ["ABC_BENCH_WORKSPACE"])
    spec = importlib.util.spec_from_file_location(
        "abc_bench_candidate_solution",
        workspace / "solution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

#### `setUpClass`

Loads solution once for all tests:

```python
@classmethod
def setUpClass(cls):
    cls.solution = load_solution_module()
```

#### Test Methods

```python
def test_function_name_basic(self):
    """Test basic functionality."""
    result = self.solution.function_name(...)
    self.assertEqual(result, expected)

def test_function_name_reference(self):
    """Test against known reference values."""
    # Use assertAlmostEqual for floating point
    self.assertAlmostEqual(actual, expected, places=6)
```

### Assertion Methods

| Method | Use Case |
|--------|----------|
| `assertEqual(a, b)` | Exact equality |
| `assertNotEqual(a, b)` | Inequality |
| `assertTrue(x)` | Truthiness |
| `assertFalse(x)` | Falsiness |
| `assertAlmostEqual(a, b, places=6)` | Float comparison |
| `assertLess(a, b)` | Ordering |
| `assertRaises(Error, fn)` | Exception testing |

---

## PASS/FAIL Determination

The runner uses **both** criteria:

```python
# runner.py:_run_verification()
status_lines = [line.strip() for line in log_content.splitlines()
                if line.strip() in {"PASS", "FAIL"}]
final_status = status_lines[-1] if status_lines else "FAIL"
return "PASS" if final_status == "PASS" and returncode == 0 else "FAIL"
```

### Requirements for PASS

1. **Exit code = 0** — `verify_solution.sh` succeeded
2. **Last PASS/FAIL line = "PASS"** — Test output ends with PASS

### Results in FAIL if

- Exit code ≠ 0 (script error, test failure)
- Last PASS/FAIL line = "FAIL"
- No PASS/FAIL line found (defaults to FAIL)

---

## Environment Variables

### Provided to verify_solution.sh

| Variable | Description |
|----------|-------------|
| `ABC_BENCH_WORKSPACE` | Path to agent's workspace (contains solution.py) |
| `ABC_BENCH_TASK_ENV` | Path to task conda environment (if created) |

### Available in test_solution.py

```python
import os
workspace = os.environ["ABC_BENCH_WORKSPACE"]
task_env = os.environ.get("ABC_BENCH_TASK_ENV")
```

---

## Example: Hello World Task

### verify_solution.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${ABC_BENCH_WORKSPACE}"

[[ -f "${WORKSPACE}/solution.py" ]] || { echo "FAIL"; exit 1; }

export ABC_BENCH_WORKSPACE="${WORKSPACE}"

TASK_ENV="${ABC_BENCH_TASK_ENV:-${WORKSPACE}/abc-bench-task-env}"
if [[ -x "${TASK_ENV}/bin/python" ]]; then
    PYTHON="${TASK_ENV}/bin/python"
else
    PYTHON="python"
fi

if "${PYTHON}" "${SCRIPT_DIR}/test_solution.py"; then
    echo "PASS"
else
    echo "FAIL"
    exit 1
fi
```

### test_solution.py

```python
import importlib.util
import os
import unittest
from pathlib import Path


def load_solution_module():
    workspace = Path(os.environ["ABC_BENCH_WORKSPACE"])
    spec = importlib.util.spec_from_file_location(
        "abc_bench_candidate_solution",
        workspace / "solution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solution = load_solution_module()

    def test_hello_world_exists(self):
        """Test that hello_world function exists."""
        self.assertTrue(hasattr(self.solution, "hello_world"))
        self.assertTrue(callable(self.solution.hello_world))

    def test_hello_world_returns_string(self):
        """Test that hello_world returns a string."""
        result = self.solution.hello_world()
        self.assertIsInstance(result, str)

    def test_hello_world_correct_output(self):
        """Test that hello_world returns exact expected string."""
        result = self.solution.hello_world()
        self.assertEqual(result, "Hello, world!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

---

## Example: Scientific Task (eos-metropolis-1953)

### test_solution.py (excerpt)

```python
class TestSolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.solution = load_solution_module()

    def test_minimum_image_delta(self):
        """Test minimum image convention."""
        self.assertAlmostEqual(
            cls.solution.minimum_image_delta(0.0, 10.0), 0.0
        )
        self.assertAlmostEqual(
            cls.solution.minimum_image_delta(4.9, 10.0), 4.9
        )
        self.assertAlmostEqual(
            cls.solution.minimum_image_delta(5.1, 10.0), -4.9
        )

    def test_periodic_distance_squared(self):
        """Test periodic distance calculation."""
        # Known reference values
        self.assertAlmostEqual(
            cls.solution.periodic_distance_squared((0, 0), (1, 1), 10.0),
            2.0
        )

    def test_has_overlap(self):
        """Test overlap detection."""
        positions = [(0.0, 0.0), (5.0, 5.0)]
        # No overlap
        self.assertFalse(cls.solution.has_overlap(
            positions, 0, (2.0, 2.0), 1.0, 10.0
        ))
        # Overlap
        self.assertTrue(cls.solution.has_overlap(
            positions, 0, (0.5, 0.5), 1.0, 10.0
        ))

    def test_run_simulation_deterministic(self):
        """Test full simulation with fixed seed."""
        initial = [(1.0, 1.0), (3.0, 3.0), (5.0, 5.0)]
        result = cls.solution.run_simulation(
            initial_positions=initial,
            diameter=1.0,
            box_length=10.0,
            max_displacement=0.5,
            seed=42,
            steps=100,
        )
        # Reference values from known implementation
        self.assertEqual(result["attempted_moves"], 100.0)
        self.assertAlmostEqual(result["acceptance_fraction"], 0.42, places=2)
        self.assertAlmostEqual(result["final_x_sum"], 9.123456, places=3)
```

---

## Running Verification Manually

```bash
cd results/20260615T120000_eos-metropolis-1953_model/workspace
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
```

Output:
```
running test_solution.py
test_minimum_image_delta (__main__.TestSolution) ... ok
test_periodic_distance_squared (__main__.TestSolution) ... ok
test_has_overlap (__main__.TestSolution) ... ok
test_attempt_move (__main__.TestSolution) ... ok
test_run_simulation_deterministic (__main__.TestSolution) ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.05s

OK
PASS
```

---

## Best Practices

### Test Design

1. **Test public API only** — Test function signatures from `solution.py`
2. **Use fixed seeds** — For deterministic algorithms, test exact reference values
3. **Test edge cases** — Empty inputs, boundary values, invalid inputs
4. **Test basic functionality** — Sanity checks before reference tests
5. **Include reference implementation** — Embed known-correct logic in tests

### Numerical Testing

```python
# Use assertAlmostEqual for floating point
self.assertAlmostEqual(actual, expected, places=6)

# For arrays, compare element-wise or use numpy.testing
import numpy as np
np.testing.assert_allclose(actual_array, expected_array, rtol=1e-5)
```

### Determinism

```python
# Always specify seed for random operations
result = self.solution.run_simulation(
    ...,
    seed=42,  # Fixed seed
    steps=100,
)
```

---

## Pitfalls

| Issue | Solution |
|-------|----------|
| PASS/FAIL not detected | Last line must be exactly `PASS` or `FAIL` |
| Tests use system python | `verify_solution.sh` must use task conda env |
| Import errors | Use `importlib.util` with `ABC_BENCH_WORKSPACE` |
| Non-deterministic failures | Use fixed seeds, avoid `random.random()` without seed |
| Floating point failures | Use `assertAlmostEqual` with appropriate `places` |
| Agent sees tests | Verification dir injected **after** agent finishes |

---

## Debugging Verification

### View Verification Log

```bash
cat results/*/verify.log
```

### Run Verification Manually

```bash
cd results/.../workspace
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
```

### Check Test Output

```bash
cd results/.../verification
python test_solution.py -v
```

---

## Integration with Runner

The runner handles verification automatically:

```python
# In BenchmarkRunner.run()

# 1. After harness completes, inject verification
shutil.copytree(task.verification_dir, result_dir / "verification")

# 2. Run verification in container
verify_result = self._run_verification(
    harness=harness,
    workspace=workspace,
    verification_dir=verification_dir,
    log_file=result_dir / "verify.log",
)

# 3. verify_result is "PASS" or "FAIL"
```

---

## Custom Verification

For non-Python tasks, you can implement custom verification:

### verify_solution.sh (custom)

```bash
#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${ABC_BENCH_WORKSPACE}"

# Custom verification logic
if [[ -f "${WORKSPACE}/output.txt" ]]; then
    # Compare with reference
    if diff -q "${WORKSPACE}/output.txt" /reference/output.txt; then
        echo "PASS"
        exit 0
    fi
fi

echo "FAIL"
exit 1
```

The only requirement: print `PASS` or `FAIL` as last line, exit 0/1.

---

## Next Steps

- [Task Structure](task-structure.md) — Complete task specification
- [Adding Tasks](../developer-guide/adding-tasks.md) — Create tasks with verification
- [Architecture](../developer-guide/architecture.md) — System overview
