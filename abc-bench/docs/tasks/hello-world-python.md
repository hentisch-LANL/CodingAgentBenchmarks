# Hello World Python

Minimal smoke test task for verifying the ABC Bench pipeline.

## Overview

| Property | Value |
|----------|-------|
| **Task ID** | `hello-world-python` |
| **Task Group** | `smoke_test` |
| **Type** | Smoke test |
| **Dependencies** | Python stdlib only |
| **Expected Duration** | < 30 seconds |
| **Verification** | Exact string match |

## Task Description

Implement a function `hello_world()` that returns exactly:

```
Hello, world!
```

The prompt asks for the classic greeting with specific formatting:

- Capital `H`
- Lowercase `world`
- Comma after `Hello`
- Exclamation point at the end
- No leading/trailing whitespace

## Files

### `prompt.md`
Task description visible to the agent. Specifies the exact string to return and that only stdlib is allowed.

### `workspace/solution.py`
```python
"""Minimal hello-world solution stub for ABC Bench."""

from __future__ import annotations


def hello_world() -> str:
    """Return the classic hello-world greeting."""
    raise NotImplementedError
```

### `verification/test_solution.py`
Hidden test suite checking:
1. `hello_world()` function exists and is callable
2. Returns exactly `"Hello, world!"`

```python
EXPECTED = "Hello, world!"

class TestHelloWorld(unittest.TestCase):
    def test_function_exists(self):
        self.assertTrue(hasattr(self.solution, "hello_world"))
        self.assertTrue(callable(self.solution.hello_world))

    def test_returns_exact_greeting(self):
        self.assertEqual(self.solution.hello_world(), EXPECTED)
```

## Running the Task

```bash
abc-bench run \
  --task smoke_test/hello-world-python \
  --agent opencode-default
```

## Expected Result

**PASS** — Agent implements `hello_world()` returning `"Hello, world!"`

**FAIL** — Function missing, wrong return value, or exception

## Purpose

This task verifies the complete end-to-end pipeline:
- Container build and execution
- Model proxy and API authentication
- Agent execution in workspace
- Hidden verification injection
- Result parsing and PASS/FAIL determination

It's the "hello world" of the benchmark suite itself — if this fails, nothing else will work.

## Manual Testing

```bash
cd tasks/smoke_test/hello-world-python/workspace
# Edit solution.py
export ABC_BENCH_WORKSPACE=$(pwd)
../verification/verify_solution.sh
# Should output: PASS
```
