#!/usr/bin/env python3
"""Collect small GitLab artifacts from an abc-bench results archive."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any


INCONCLUSIVE_STATUSES = {"HARNESS_ERROR", "ENDPOINT_ERROR"}


def newest_metadata(results_root: Path, started_at: float) -> Path | None:
    if not results_root.is_dir():
        return None
    candidates = [path for path in results_root.rglob("metadata.json") if path.is_file()]
    if not candidates:
        return None
    recent = [path for path in candidates if path.stat().st_mtime >= started_at - 5]
    if recent:
        candidates = recent
    return max(candidates, key=lambda path: path.stat().st_mtime)


def recent_metadata(results_root: Path, started_at: float, task_group: str | None) -> list[Path]:
    if not results_root.is_dir():
        return []
    candidates = [path for path in results_root.rglob("metadata.json") if path.is_file()]
    if task_group is not None:
        filtered = []
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
            except (json.JSONDecodeError, OSError):
                continue
            if metadata.get("task_group") == task_group and path.stat().st_mtime >= started_at - 5:
                filtered.append(path)
        return sorted(filtered, key=lambda path: path.stat().st_mtime, reverse=True)
    return sorted(
        [path for path in candidates if path.stat().st_mtime >= started_at - 5],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def lingering_inconclusive_metadata(results_root: Path, task_group: str | None) -> list[Path]:
    if not results_root.is_dir():
        return []
    candidates = [path for path in results_root.rglob("metadata.json") if path.is_file()]
    lingering: list[Path] = []
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (json.JSONDecodeError, OSError):
            continue
        if task_group is not None and metadata.get("task_group") != task_group:
            continue
        if metadata.get("status") in INCONCLUSIVE_STATUSES:
            lingering.append(path)
    return sorted(lingering, key=lambda path: path.stat().st_mtime, reverse=True)


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument("--started-at", required=True, type=float)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--task-group", default=None)
    args = parser.parse_args()

    artifacts_dir = Path(os.environ.get("ABC_CI_ARTIFACTS_DIR", "api_artifacts"))
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    resolved_results_root = args.results_root.expanduser().resolve()
    lingering_inconclusive = lingering_inconclusive_metadata(resolved_results_root, args.task_group)
    if lingering_inconclusive:
        print("WARNING: found lingering inconclusive benchmark results in archive:")
        for path in lingering_inconclusive[:10]:
            print(f"  {path.parent}")
        if len(lingering_inconclusive) > 10:
            print(f"  ... and {len(lingering_inconclusive) - 10} more")

    metadata_path = newest_metadata(resolved_results_root, args.started_at)
    if metadata_path is None:
        manifest = {
            "schema_version": 1,
            "status": "ERROR" if args.exit_code else "UNKNOWN",
            "abc_bench_exit_code": args.exit_code,
            "metadata_found": False,
            "lingering_inconclusive_count": len(lingering_inconclusive),
        }
        write_json(artifacts_dir / "manifest.json", manifest)
        print("No abc-bench metadata.json found for this run")
        return 0

    result_dir = metadata_path.parent
    shutil.copy2(metadata_path, artifacts_dir / "metadata.json")

    result_file = result_dir / "result.json"
    if result_file.is_file():
        shutil.copy2(result_file, artifacts_dir / "result.json")

    metadata = load_json(metadata_path)

    batch_metadata_paths = recent_metadata(resolved_results_root, args.started_at, args.task_group)
    batch_summary: dict[str, Any] | None = None
    if args.task_group is not None:
        statuses: dict[str, int] = {}
        task_refs: list[str] = []
        for path in batch_metadata_paths:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    entry = json.load(handle)
            except (json.JSONDecodeError, OSError):
                continue
            status = entry.get("status", "UNKNOWN")
            statuses[status] = statuses.get(status, 0) + 1
            task_ref = entry.get("task_ref")
            if task_ref:
                task_refs.append(task_ref)
        batch_summary = {
            "task_group": args.task_group,
            "run_count": len(task_refs),
            "task_refs": sorted(task_refs),
            "statuses": dict(sorted(statuses.items())),
        }

    agent_block = metadata.get("agent") if isinstance(metadata.get("agent"), dict) else {}
    result_data = load_json(result_file) if result_file.is_file() else {}

    manifest = {
        "schema_version": 1,
        "metadata_found": True,
        "result_dir": str(result_dir),
        "task": metadata.get("task"),
        "task_group": metadata.get("task_group"),
        "task_ref": metadata.get("task_ref"),
        "agent": agent_block.get("name") or result_data.get("agent"),
        "harness": agent_block.get("harness") or result_data.get("harness"),
        "models": agent_block.get("models") or result_data.get("models"),
        "agent_archive_dir": metadata.get("agent_archive_dir"),
        "status": metadata.get("status"),
        "total_cost": result_data.get("total_cost", metadata.get("usage", {}).get("totals", {}).get("total_cost", 0.0)),
        "abc_bench_exit_code": args.exit_code,
        "lingering_inconclusive_count": len(lingering_inconclusive),
    }
    if batch_summary is not None:
        manifest["batch"] = batch_summary
    write_json(artifacts_dir / "manifest.json", manifest)
    print(f"Collected artifacts from: {result_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
