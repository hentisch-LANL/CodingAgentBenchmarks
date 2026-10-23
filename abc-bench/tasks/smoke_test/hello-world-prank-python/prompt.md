# Task: Hello World in Python

## Overview

Implement the simplest possible Python function for this benchmark: return the classic greeting string `Hello, world!`.

This task is intentionally minimal. It exists to verify that the ABC Bench harness, container runtime, model proxy, agent execution, workspace handling, and hidden verification all work end-to-end before running more demanding scientific tasks.

## Problem Description

Complete the function in `solution.py` so that it returns exactly:

```text
Hello, world!
```

## Implementation Requirements

Complete the function in `solution.py`. The file contains the function signature and a short docstring.

### Critical Requirements

**Use only the Python standard library.** No external dependencies are needed.

**Complete only `solution.py`.** Do not modify function signatures or create additional files.

**Return the exact string:**
- Capital `H`
- Lowercase `world`
- Comma after `Hello`
- Exclamation point at the end
- No leading or trailing whitespace

## Submission

Your completed `solution.py` file in this workspace will be evaluated by a hidden test suite.

Before finishing, you may run a quick check such as:

```bash
python -c "import solution; print(solution.hello_world())"
```

The tests will check:
1. `solution.py` can be imported
2. `hello_world()` exists
3. `hello_world()` returns exactly `Hello, world!`

Good luck!
