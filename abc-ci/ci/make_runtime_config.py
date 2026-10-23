#!/usr/bin/env python3
"""Create a job-local abc-bench config with a scratch results directory."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: make_runtime_config.py INPUT_CONFIG OUTPUT_CONFIG RESULTS_DIR",
            file=sys.stderr,
        )
        return 2

    input_config = Path(sys.argv[1]).expanduser().resolve()
    output_config = Path(sys.argv[2]).expanduser().resolve()
    results_dir = Path(sys.argv[3]).expanduser().resolve()

    with input_config.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    defaults = dict(data.get("defaults") or {})
    defaults["results_dir"] = str(results_dir)
    data["defaults"] = defaults

    output_config.parent.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    with output_config.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)

    print(output_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
