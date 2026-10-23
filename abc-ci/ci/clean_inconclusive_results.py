#!/usr/bin/env python3
"""Remove inconclusive abc-bench results created during the current CI run."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


INCONCLUSIVE_STATUSES = {"HARNESS_ERROR", "ENDPOINT_ERROR"}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _matches_scope(metadata: dict[str, Any], task_group: str | None) -> bool:
    if task_group is None:
        return True
    return metadata.get("task_group") == task_group


def _recent_metadata(results_root: Path, started_at: float, task_group: str | None) -> list[Path]:
    if not results_root.is_dir():
        return []

    matches: list[Path] = []
    for path in results_root.rglob("metadata.json"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < started_at - 5:
                continue
        except OSError:
            continue
        metadata = _load_json(path)
        if metadata is None or not _matches_scope(metadata, task_group):
            continue
        matches.append(path)
    return sorted(matches, key=lambda item: item.stat().st_mtime, reverse=True)


def _inconclusive_entries(metadata_paths: list[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in metadata_paths:
        metadata = _load_json(path)
        if metadata is None:
            continue
        status = metadata.get("status")
        if status not in INCONCLUSIVE_STATUSES:
            continue
        entries.append(
            {
                "metadata_path": str(path),
                "result_dir": str(path.parent),
                "task_ref": metadata.get("task_ref"),
                "status": status,
            }
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument("--started-at", required=True, type=float)
    parser.add_argument("--task-group", default=None)
    parser.add_argument("--summary-out", default=None, type=Path)
    args = parser.parse_args()

    results_root = args.results_root.expanduser().resolve()
    metadata_paths = _recent_metadata(results_root, args.started_at, args.task_group)
    inconclusive = _inconclusive_entries(metadata_paths)

    removed: list[dict[str, Any]] = []
    for entry in inconclusive:
        result_dir = Path(entry["result_dir"])
        if not result_dir.exists():
            continue
        shutil.rmtree(result_dir)
        removed.append(entry)
        task_ref = entry.get("task_ref") or result_dir.name
        print(f"Removed inconclusive result: {task_ref} ({entry['status']})")

    summary = {
        "schema_version": 1,
        "inconclusive_statuses": sorted(INCONCLUSIVE_STATUSES),
        "removed_count": len(removed),
        "removed": removed,
    }

    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_out.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
