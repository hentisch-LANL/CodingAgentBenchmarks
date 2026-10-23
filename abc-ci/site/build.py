#!/usr/bin/env python3
"""Build a simple agent-centric static site from archived abc-bench results."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import math

import numpy as np
from PIL import Image


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _run_sort_key(run: dict[str, Any]) -> tuple[str, str]:
    metadata = run["metadata"]
    result = run["result"]
    return (
        str(metadata.get("ended_at") or result.get("timestamp") or ""),
        str(run["run_dir"]),
    )


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_total_cost(run: dict[str, Any]) -> float | None:
    result = run["result"]
    metadata = run["metadata"]

    cost = _coerce_float(result.get("total_cost"))
    if cost is not None:
        return cost

    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}
    return _coerce_float(totals.get("total_cost"))


def _extract_from_totals(run: dict[str, Any], key: str) -> float | None:
    for source in ("result", "metadata"):
        block = run.get(source)
        if not isinstance(block, dict):
            continue
        usage = block.get("usage") if isinstance(block.get("usage"), dict) else {}
        totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}
        value = _coerce_float(totals.get(key))
        if value is not None:
            return value
    return None


def _extract_total_tokens(run: dict[str, Any]) -> float | None:
    return _extract_from_totals(run, "total_tokens")


def _extract_tokens_per_second(run: dict[str, Any]) -> float | None:
    return _extract_from_totals(run, "tokens_per_second")


def _format_cost(cost: float | None) -> str:
    if cost is None:
        return "n/a"

    if cost < 0:
        return f"-${abs(cost):,.2f}"
    return f"${cost:,.2f}"


def _extract_elapsed_seconds(run: dict[str, Any]) -> float | None:
    return _coerce_float(run["metadata"].get("elapsed_seconds"))


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"

    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"


_Z_95 = 1.959963984540054  # standard-normal critical value for a 95% interval


def _dround(value: float | None, ndigits: int = 6) -> float | None:
    """Decimal-safe round-half-up used to normalize float noise before display.

    Averages computed here and in the browser (recompute.js) can differ in the
    last ULP purely from summation order, which is enough to flip a display tie
    (e.g. 0.5950000000000001 -> "$0.60" vs 0.595 -> "$0.59"). Both sides round
    every mean/percentage to a fixed decimal grid first, so the server- and
    client-rendered values are always identical. Kept well above display
    precision (6 dp) so it never distorts the shown 0-2 decimal figures beyond
    the tie it resolves.
    """
    if value is None:
        return None
    factor = 10 ** ndigits
    # Half-up on the scaled value; math.floor(x + 0.5) rounds half away from
    # zero for the non-negative magnitudes we deal with, matching recompute.js.
    if value >= 0:
        return math.floor(value * factor + 0.5) / factor
    return -math.floor(-value * factor + 0.5) / factor


def _wilson_proportion_ci(passed: int, total: int) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion, as fractions [0, 1].

    Chosen over the Wald/normal interval because pass-rate data routinely has
    small n and p near 0 or 1, where Wald degenerates (zero-width at the
    extremes, mass outside [0, 1]). It replaces the previous bootstrap-of-the-
    mean, which is an expensive and poor approximation of a proportion CI. The
    same formula is implemented in the browser (recompute.js) so client-side
    recomputation over selected task groups matches this server-side value.
    """
    if total <= 0:
        return None
    p = passed / total
    z = _Z_95
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (low, high)


def _mean_ci(samples: list[float]) -> tuple[float, float] | None:
    """95% normal-approximation CI for a mean, matching recompute.js.

    Used for the continuous columns (cost, tokens, tok/s, walltime). With one
    sample the interval collapses to the point value.
    """
    n = len(samples)
    if n == 0:
        return None
    mean = sum(samples) / n
    if n == 1:
        return (mean, mean)
    var = sum((x - mean) ** 2 for x in samples) / (n - 1)
    half = _Z_95 * math.sqrt(var / n)
    return (mean - half, mean + half)


def _format_percentage_ci(interval: tuple[float, float] | None) -> str:
    if interval is None:
        return "n/a"

    lower, upper = interval
    return f"[{lower:.0f}%, {upper:.0f}%]"


def _format_tokens(t: float | None) -> str:
    if t is None:
        return "n/a"
    if t >= 1000:
        return f"{t / 1000:.1f}k"
    return f"{t:.0f}"


def _format_tokens_per_second(tps: float | None) -> str:
    if tps is None:
        return "n/a"
    return f"{tps:.1f}"


def _format_ci_generic(interval: tuple[float, float] | None, unit_formatter) -> str:
    if not interval:
        return ""
    low, high = interval
    return f"[{unit_formatter(low)}, {unit_formatter(high)}]"


def _stat_span(value_html: str, tip_text: str) -> str:
    if tip_text:
        return f'<span class="stat" data-tip="{escape(tip_text)}">{value_html}</span>'
    return f'<span class="stat">{value_html}</span>'


def _status_sort_value(status: str) -> int:
    if status == "PASS":
        return 3
    if status == "FAIL":
        return 2
    if status in {"HARNESS_ERROR", "ENDPOINT_ERROR", "ERROR"}:
        return 1
    return 0


def _sortable_header(
    label_html: str,
    sort_type: str,
    *,
    th_class: str = "",
    label_href: str | None = None,
    default_sort: str | None = None,
    button_label: str | None = None,
) -> str:
    attrs = [f'data-sort-type="{escape(sort_type)}"']
    if default_sort is not None:
        attrs.append(f'data-sort-default="{escape(default_sort)}"')
        attrs.append(f'aria-sort="{escape(default_sort)}"')
    else:
        attrs.append('aria-sort="none"')

    attr_text = " ".join(attrs)
    class_attr = f' class="{th_class}"' if th_class else ""
    button_text = escape(button_label or "Sort column")
    if label_href is not None:
        content = (
            '<span class="sort-header">'
            f'<a href="{escape(label_href)}">{label_html}</a>'
            f'<button type="button" class="sort-button sort-button-inline" aria-label="{button_text}" data-sort-label="{button_text}">'
            '<span class="sort-icon" aria-hidden="true"></span>'
            "</button>"
            "</span>"
        )
    else:
        content = (
            f'<button type="button" class="sort-button sort-button-full" aria-label="{button_text}" data-sort-label="{button_text}">'
            f'<span class="sort-label">{label_html}</span>'
            '<span class="sort-icon" aria-hidden="true"></span>'
            "</button>"
        )

    return f'<th{class_attr} {attr_text}>{content}</th>'


def find_runs(archive_dir: Path) -> list[dict[str, Any]]:
    if not archive_dir.exists():
        print(f"Warning: archive directory does not exist: {archive_dir}", file=sys.stderr)
        return []

    runs: list[dict[str, Any]] = []
    for metadata_path in archive_dir.rglob("metadata.json"):
        if not metadata_path.is_file():
            continue
        metadata = _read_json(metadata_path)
        if metadata is None:
            continue
        run_dir = metadata_path.parent
        result = _read_json(run_dir / "result.json") or {}
        runs.append(
            {
                "run_dir": run_dir,
                "metadata": metadata,
                "result": result,
            }
        )
    return runs


def load_task_definitions(tasks_dir: Path) -> dict[str, Any]:
    tasks_json = tasks_dir / "tasks.json"
    data = _read_json(tasks_json)
    if data is None:
        print(f"Warning: task definitions not found at {tasks_json}", file=sys.stderr)
        return {}
    return data


def load_model_definitions(models_dir: Path) -> dict[str, Any]:
    models_json = models_dir / "models.json"
    data = _read_json(models_json)
    if data is None:
        print(f"Warning: model definitions not found at {models_json}", file=sys.stderr)
        return {}
    return data


def load_harness_definitions(harnesses_dir: Path) -> dict[str, Any]:
    harnesses_json = harnesses_dir / "harnesses.json"
    data = _read_json(harnesses_json)
    if data is None:
        print(f"Warning: harness definitions not found at {harnesses_json}", file=sys.stderr)
        return {}
    return data


def model_display_name(model_id: str, model_definitions: dict[str, Any]) -> str:
    entry = model_definitions.get(model_id)
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return model_id


def model_description(model_id: str, model_definitions: dict[str, Any]) -> str:
    entry = model_definitions.get(model_id)
    if isinstance(entry, dict) and entry.get("description"):
        return str(entry["description"])
    return ""


def model_provider(model_id: str, model_definitions: dict[str, Any]) -> str:
    entry = model_definitions.get(model_id)
    if isinstance(entry, dict) and entry.get("provider"):
        return str(entry["provider"])
    return ""


def model_logo(model_id: str, model_definitions: dict[str, Any]) -> str:
    entry = model_definitions.get(model_id)
    if isinstance(entry, dict) and entry.get("logo"):
        return str(entry["logo"])
    return ""


# Cache of per-file logo alpha analysis, keyed by resolved filesystem path.
_LOGO_ANALYSIS_CACHE: dict[str, dict[str, float]] = {}


def analyze_logo(logo_rel: str, static_dir: Path) -> dict[str, float]:
    """Analyze a logo image's alpha channel for plot layout.

    Returns fractions in [0, 1]:
      - ``inset_left`` / ``inset_right``: transparent margin along the image's
        vertical center band, used to interrupt the CI line at the logo's
        visible edge rather than its bounding box.
      - ``cx``: alpha-weighted horizontal centroid, used to align the logo's
        visual weight (not its geometric center) on the point estimate.

    Vector logos (``.svg``) have no raster alpha, so they are treated as
    full-bleed and centered: ``inset_left = inset_right = 0``, ``cx = 0.5``.
    """
    default = {"inset_left": 0.0, "inset_right": 0.0, "cx": 0.5}
    if not logo_rel:
        return default

    path = (static_dir / logo_rel).resolve()
    key = str(path)
    if key in _LOGO_ANALYSIS_CACHE:
        return _LOGO_ANALYSIS_CACHE[key]

    result = dict(default)
    if path.suffix.lower() != ".svg" and path.exists():
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
            width, height = rgba.size
            alpha = np.asarray(rgba.getchannel("A"), dtype=np.float64)  # (h, w)
            total = float(alpha.sum())
            if total > 0 and width > 0 and height > 0:
                # Alpha-weighted horizontal centroid.
                col_weight = alpha.sum(axis=0)  # per-column visible weight
                cx = float((np.arange(width) * col_weight).sum() / total)
                result["cx"] = cx / width

                # Visible extent along the vertical center band (a few rows
                # around the middle, where the CI line crosses the logo).
                band = max(1, height // 10)
                y0 = max(0, height // 2 - band // 2)
                y1 = min(height, y0 + band)
                band_cols = alpha[y0:y1, :].max(axis=0)
                visible = np.where(band_cols > 40)[0]
                if visible.size:
                    result["inset_left"] = float(visible[0]) / width
                    result["inset_right"] = float(width - 1 - visible[-1]) / width

    _LOGO_ANALYSIS_CACHE[key] = result
    return result


def harness_display_name(harness_id: str, harness_definitions: dict[str, Any]) -> str:
    entry = harness_definitions.get(harness_id)
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return harness_id


def harness_description(harness_id: str, harness_definitions: dict[str, Any]) -> str:
    entry = harness_definitions.get(harness_id)
    if isinstance(entry, dict) and entry.get("description"):
        return str(entry["description"])
    return ""


def _join_natural(items: list[str]) -> str:
    """Join items with commas and a trailing 'and': "a", "a and b", "a, b, and c"."""
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def agent_label(
    harness_id: str,
    model_ids: list[str],
    harness_definitions: dict[str, Any],
    model_definitions: dict[str, Any],
) -> str:
    """Format an agent as "[Harness Name] with [Models]".

    e.g. "OpenCode with Claude Opus 4.8" or
         "OpenCode (Hybrid) with Claude Opus 4.8 and Gemma 4 31B".
    """
    harness_name = harness_display_name(harness_id, harness_definitions)
    model_names = [model_display_name(m, model_definitions) for m in model_ids]
    joined = _join_natural(model_names)
    if not joined:
        return harness_name
    return f"{harness_name} with {joined}"


def task_display_name(task_ref: str, task_definitions: dict[str, Any]) -> str:
    task_id = task_ref.split("/", 1)[-1]
    if task_ref in task_definitions:
        return str(task_definitions[task_ref].get("name") or task_ref)
    if task_id in task_definitions:
        return str(task_definitions[task_id].get("name") or task_ref)
    return task_ref


def task_description(task_ref: str, task_definitions: dict[str, Any]) -> str:
    task_id = task_ref.split("/", 1)[-1]
    if task_ref in task_definitions:
        return str(task_definitions[task_ref].get("description") or "")
    if task_id in task_definitions:
        return str(task_definitions[task_id].get("description") or "")
    return ""


def _task_group_name(task_ref: str, task_definitions: dict[str, Any]) -> str:
    task_id = task_ref.split("/", 1)[-1]
    if task_ref in task_definitions:
        group = task_definitions[task_ref].get("group")
        if group:
            return str(group)
    if task_id in task_definitions:
        group = task_definitions[task_id].get("group")
        if group:
            return str(group)

    if "/" in task_ref:
        namespace, _, _ = task_ref.partition("/")
        if namespace:
            return namespace

    prefix, _, _ = task_id.partition("-")
    return prefix or task_id


def _group_tasks(all_tasks: list[str], task_definitions: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for task_ref in all_tasks:
        group_name = _task_group_name(task_ref, task_definitions)
        if group_name not in grouped:
            grouped[group_name] = []
            order.append(group_name)
        grouped[group_name].append(task_ref)

    return [{"name": group_name, "tasks": grouped[group_name]} for group_name in order]


def _agent_info(run: dict[str, Any]) -> dict[str, Any]:
    metadata = run["metadata"]
    result = run["result"]
    agent_block = metadata.get("agent") if isinstance(metadata.get("agent"), dict) else {}
    agent_name = str(result.get("agent") or agent_block.get("name") or "unknown-agent")
    harness = str(result.get("harness") or agent_block.get("harness") or "unknown-harness")

    model_names = result.get("models")
    if not isinstance(model_names, list) or not model_names:
        model_names = agent_block.get("models") if isinstance(agent_block.get("models"), list) else []
    model_names = [str(name) for name in model_names if str(name)]

    model_label = ", ".join(model_names) if model_names else "no models recorded"
    secondary_label = f"{harness} | {model_label}"

    return {
        "agent": agent_name,
        "harness": harness,
        "models": model_names,
        "secondary_label": secondary_label,
        "archive_dir": str(metadata.get("agent_archive_dir") or ""),
    }


def aggregate_runs(
    runs: list[dict[str, Any]],
    task_definitions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_definitions = task_definitions or {}
    by_agent: dict[str, dict[str, Any]] = {}
    all_tasks: set[str] = set()

    for run in sorted(runs, key=_run_sort_key):
        metadata = run["metadata"]
        result = run["result"]
        task_ref = str(result.get("task_ref") or metadata.get("task_ref") or "")
        if not task_ref:
            continue
        info = _agent_info(run)
        agent_name = info["agent"]
        all_tasks.add(task_ref)

        entry = by_agent.setdefault(
            agent_name,
            {
                "agent": agent_name,
                "harness": info["harness"],
                "models": info["models"],
                "secondary_label": info["secondary_label"],
                "archive_dir": info["archive_dir"],
                "attempts": [],
                "tasks": {},
                "latest_run": run,
            },
        )
        if _run_sort_key(run) >= _run_sort_key(entry["latest_run"]):
            entry["harness"] = info["harness"]
            entry["models"] = info["models"]
            entry["secondary_label"] = info["secondary_label"]
            entry["archive_dir"] = info["archive_dir"]
            entry["latest_run"] = run

        status = str(result.get("status") or metadata.get("status") or "UNKNOWN")
        entry["attempts"].append(
            {
                "task_ref": task_ref,
                "group": _task_group_name(task_ref, task_definitions),
                "status": status,
                "passed": status == "PASS",
                "total_cost": _extract_total_cost(run),
                "elapsed_seconds": _extract_elapsed_seconds(run),
                "total_tokens": _extract_total_tokens(run),
                "tokens_per_second": _extract_tokens_per_second(run),
            }
        )

    for agent_name, entry in by_agent.items():
        attempts = entry["attempts"]

        # Per-task rollups. Attempts are appended in ascending sort order, so
        # the last-seen attempt per task is the latest.
        task_rollups: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            task_ref = attempt["task_ref"]
            rollup = task_rollups.setdefault(
                task_ref,
                {"attempts": 0, "passed_count": 0, "status": attempt["status"], "_costs": [], "_tokens": []},
            )
            rollup["attempts"] += 1
            if attempt["passed"]:
                rollup["passed_count"] += 1
            rollup["status"] = attempt["status"]
            if attempt["total_cost"] is not None:
                rollup["_costs"].append(attempt["total_cost"])
            if attempt["total_tokens"] is not None:
                rollup["_tokens"].append(attempt["total_tokens"])

        tasks: dict[str, dict[str, Any]] = {}
        for task_ref, rollup in task_rollups.items():
            count = rollup["attempts"]
            passed_count = rollup["passed_count"]
            costs = rollup["_costs"]
            tokens = rollup["_tokens"]
            tasks[task_ref] = {
                "attempts": count,
                "passed_count": passed_count,
                "status": rollup["status"],
                "pass_rate": (passed_count / count * 100.0) if count else 0.0,
                "passed": passed_count * 2 >= count and count > 0,
                "total_cost": (sum(costs) / len(costs)) if costs else None,
                "total_tokens": (sum(tokens) / len(tokens)) if tokens else None,
            }
        entry["tasks"] = tasks

        attempt_count = len(attempts)
        passed_attempts = sum(1 for a in attempts if a["passed"])
        recorded_costs = [a["total_cost"] for a in attempts if a["total_cost"] is not None]
        recorded_durations = [a["elapsed_seconds"] for a in attempts if a["elapsed_seconds"] is not None]
        recorded_tokens = [a["total_tokens"] for a in attempts if a["total_tokens"] is not None]
        recorded_tps = [a["tokens_per_second"] for a in attempts if a["tokens_per_second"] is not None]

        entry["total_count"] = len(all_tasks)
        entry["ran_count"] = len(tasks)
        entry["attempt_count"] = attempt_count
        entry["passed_count"] = passed_attempts
        entry["pass_rate"] = (passed_attempts / attempt_count * 100.0) if attempt_count else 0.0

        pass_ci = _wilson_proportion_ci(passed_attempts, attempt_count)
        if pass_ci is not None:
            entry["pass_rate_ci"] = (pass_ci[0] * 100.0, pass_ci[1] * 100.0)
        else:
            entry["pass_rate_ci"] = None

        entry["average_cost"] = _dround((sum(recorded_costs) / len(recorded_costs)) if recorded_costs else None)
        entry["average_walltime"] = _dround((sum(recorded_durations) / len(recorded_durations)) if recorded_durations else None)
        entry["average_tokens"] = _dround((sum(recorded_tokens) / len(recorded_tokens)) if recorded_tokens else None)
        entry["average_tokens_per_second"] = _dround((sum(recorded_tps) / len(recorded_tps)) if recorded_tps else None)
        entry["cost_ci"] = _mean_ci(recorded_costs)
        entry["walltime_ci"] = _mean_ci(recorded_durations)
        entry["tokens_ci"] = _mean_ci(recorded_tokens)
        entry["tokens_per_second_ci"] = _mean_ci(recorded_tps)

        # Per-group raw breakdown for client-side recomputation. The browser
        # sums only the selected groups' counts/samples to recompute every
        # statistic (see recompute.js); keeping this here is the single source
        # of the raw numbers so server- and client-rendered values agree.
        group_breakdown: dict[str, dict[str, Any]] = {}
        for attempt in attempts:
            g = attempt["group"]
            gb = group_breakdown.setdefault(
                g,
                {
                    "attempt_count": 0,
                    "passed_count": 0,
                    "ran_tasks": set(),
                    "costs": [],
                    "durations": [],
                    "tokens": [],
                    "tps": [],
                },
            )
            gb["attempt_count"] += 1
            if attempt["passed"]:
                gb["passed_count"] += 1
            gb["ran_tasks"].add(attempt["task_ref"])
            if attempt["total_cost"] is not None:
                gb["costs"].append(attempt["total_cost"])
            if attempt["elapsed_seconds"] is not None:
                gb["durations"].append(attempt["elapsed_seconds"])
            if attempt["total_tokens"] is not None:
                gb["tokens"].append(attempt["total_tokens"])
            if attempt["tokens_per_second"] is not None:
                gb["tps"].append(attempt["tokens_per_second"])
        for gb in group_breakdown.values():
            gb["ran_count"] = len(gb["ran_tasks"])
            del gb["ran_tasks"]
        entry["group_breakdown"] = group_breakdown

    max_ran_count = max((entry["ran_count"] for entry in by_agent.values()), default=0)
    for entry in by_agent.values():
        entry["max_ran_count"] = max_ran_count
        entry["completeness"] = (entry["ran_count"] / max_ran_count * 100.0) if max_ran_count else 0.0

    return {
        "agents": by_agent,
        "all_tasks": sorted(all_tasks),
    }


def _dimension_views(aggregated: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Group aggregated agents by model id and by harness id.

    Returns {"models": {model_id: view}, "harnesses": {harness_id: view}}, where each
    view collects the agents that use that model/harness plus summary stats derived from
    those agents' per-task results.
    """
    models: dict[str, dict[str, Any]] = {}
    harnesses: dict[str, dict[str, Any]] = {}

    for agent_name, agent_data in aggregated["agents"].items():
        harness = agent_data["harness"]
        h_view = harnesses.setdefault(harness, {"id": harness, "agents": [], "models": set()})
        h_view["agents"].append(agent_data)
        for model in agent_data["models"]:
            h_view["models"].add(model)
            m_view = models.setdefault(model, {"id": model, "agents": [], "harnesses": set()})
            m_view["agents"].append(agent_data)
            m_view["harnesses"].add(harness)

    for view in list(models.values()) + list(harnesses.values()):
        agents = view["agents"]
        pass_rates = [a["pass_rate"] for a in agents]
        completeness = [a["completeness"] for a in agents]
        view["agent_count"] = len(agents)
        view["avg_pass_rate"] = (sum(pass_rates) / len(pass_rates)) if pass_rates else 0.0
        view["best_pass_rate"] = max(pass_rates) if pass_rates else 0.0
        view["avg_completeness"] = (sum(completeness) / len(completeness)) if completeness else 0.0

    return {"models": models, "harnesses": harnesses}



def _status_cell(status: str) -> str:
    if status == "PASS":
        return '<td class="numeric status-pass">Pass</td>'
    if status == "FAIL":
        return '<td class="numeric status-fail">Fail</td>'
    if status in {"HARNESS_ERROR", "ENDPOINT_ERROR", "ERROR"}:
        return f'<td class="numeric status-fail">{escape(status)}</td>'
    return f'<td class="numeric status-muted">{escape(status)}</td>'


def _task_filename(task_ref: str) -> str:
    return task_ref.replace("/", "_").replace(":", "_") + ".html"


def _agent_filename(agent_name: str) -> str:
    return agent_name.replace("/", "_").replace(":", "_") + ".html"


def _model_filename(model_id: str) -> str:
    return model_id.replace("/", "_").replace(":", "_") + ".html"


def _harness_filename(harness_id: str) -> str:
    return harness_id.replace("/", "_").replace(":", "_") + ".html"


def _logo_img(logo: str, alt: str, *, css_class: str, prefix: str = "") -> str:
    """Return an <img> tag for a logo path, or empty string if none."""
    if not logo:
        return ""
    return f'<img class="{escape(css_class)}" src="{escape(prefix + logo)}" alt="{escape(alt)}">'


def generate_agent_detail_page(agent_name: str, agent_data: dict[str, Any], all_tasks: list[str], output_dir: Path, task_definitions: dict[str, Any], model_definitions: dict[str, Any] | None = None, harness_definitions: dict[str, Any] | None = None) -> None:
    model_definitions = model_definitions or {}
    harness_definitions = harness_definitions or {}
    label = agent_label(agent_data["harness"], agent_data["models"], harness_definitions, model_definitions)
    harness_id = agent_data["harness"]
    harness_cell = (
        f'<a href="../harnesses/{_harness_filename(harness_id)}">{escape(harness_display_name(harness_id, harness_definitions))}</a>'
        if harness_id else escape(harness_display_name(harness_id, harness_definitions))
    )
    model_cell = ", ".join(
        f'<a href="../models/{_model_filename(m)}">{escape(model_display_name(m, model_definitions))}</a>'
        for m in agent_data["models"]
    ) or "n/a"
    result_rows = []
    for task_ref in all_tasks:
        task_result = agent_data["tasks"].get(task_ref)
        display_name = task_display_name(task_ref, task_definitions)
        if task_result:
            status_html = _status_cell(task_result["status"])
            cost_text = _format_cost(task_result["total_cost"])
            result_rows.append(
                f"""      <tr>
        <td><a href=\"../tasks/{_task_filename(task_ref)}\">{escape(display_name)}</a></td>
        <td>{escape(task_ref)}</td>
        {status_html}
        <td class=\"numeric\">{cost_text}</td>
      </tr>"""
            )

    if not result_rows:
        result_rows.append('      <tr><td colspan="4">No results available</td></tr>')

    timestamp = datetime.now().strftime("%I:%M %p, %-m/%-d/%Y").lstrip("0")
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>{escape(label)} - abc-ci</title>
  <link rel=\"stylesheet\" href=\"../style.css\">
</head>
<body>
  <div class=\"page\">
    <section class=\"report-frame\">
      <div class=\"report-nav\">
        <div class=\"breadcrumbs\">
          <a href=\"../index.html\">Leaderboard</a>
          <span>&gt;</span>
          <span>{escape(label)}</span>
        </div>
        <a href=\"../index.html\">Back to Leaderboard</a>
      </div>

      <h1>{escape(label)}</h1>
      <p class=\"metadata\">Agent ID: {escape(agent_name)}</p>

      <table class=\"summary-table\">
        <tbody>
          <tr><th>Agent</th><td>{escape(agent_name)}</td></tr>
          <tr><th>Harness</th><td>{harness_cell}</td></tr>
          <tr><th>Models</th><td>{model_cell}</td></tr>
          <tr><th>Pass Rate</th><td>{_stat_span(f"{agent_data['pass_rate']:.0f}%", _format_percentage_ci(agent_data['pass_rate_ci']) if agent_data['pass_rate_ci'] else "")}</td></tr>
          <tr><th>Completeness</th><td>{agent_data['completeness']:.0f}%</td></tr>
          <tr><th>Attempts Passed</th><td>{agent_data['passed_count']} / {agent_data['attempt_count']}</td></tr>
          <tr><th>Tasks Run</th><td>{agent_data['ran_count']} / {agent_data['max_ran_count']}</td></tr>
          <tr><th>Average Tokens</th><td>{_stat_span(_format_tokens(agent_data['average_tokens']), _format_ci_generic(agent_data['tokens_ci'], _format_tokens))}</td></tr>
          <tr><th>Average Tok/s</th><td>{_stat_span(_format_tokens_per_second(agent_data['average_tokens_per_second']), _format_ci_generic(agent_data['tokens_per_second_ci'], _format_tokens_per_second))}</td></tr>
          <tr><th>Average Cost</th><td>{_stat_span(_format_cost(agent_data['average_cost']), _format_ci_generic(agent_data['cost_ci'], _format_cost))}</td></tr>
          <tr><th>Average Walltime</th><td>{_stat_span(_format_duration(agent_data['average_walltime']), _format_ci_generic(agent_data['walltime_ci'], _format_duration))}</td></tr>
        </tbody>
      </table>

      <h2>Benchmark Results</h2>
      <div class=\"table-scroll\">
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Task Ref</th>
              <th class=\"numeric\">Status</th>
              <th class=\"numeric\">Cost</th>
            </tr>
          </thead>
          <tbody>
{''.join(result_rows)}
          </tbody>
        </table>
      </div>

      <p class=\"table-note\">Last Updated: {timestamp}</p>
    </section>
  </div>
</body>
</html>"""

    agent_output_dir = output_dir / "agents"
    agent_output_dir.mkdir(parents=True, exist_ok=True)
    (agent_output_dir / _agent_filename(agent_name)).write_text(html, encoding="utf-8")


def generate_task_detail_page(task_ref: str, aggregated: dict[str, Any], task_definitions: dict[str, Any], output_dir: Path, model_definitions: dict[str, Any] | None = None, harness_definitions: dict[str, Any] | None = None) -> None:
    model_definitions = model_definitions or {}
    harness_definitions = harness_definitions or {}
    rows = []
    total_agents = 0
    passed_agents = 0
    for agent_name, agent_data in sorted(aggregated["agents"].items()):
        task_result = agent_data["tasks"].get(task_ref)
        if not task_result:
            continue
        total_agents += 1
        if task_result["passed"]:
            passed_agents += 1
        label = agent_label(agent_data["harness"], agent_data["models"], harness_definitions, model_definitions)
        rows.append(
            f"""      <tr>
        <td><a href=\"../agents/{_agent_filename(agent_name)}\">{escape(label)}</a></td>
        <td>{escape(agent_name)}</td>
        {_status_cell(task_result['status'])}
        <td class=\"numeric\">{_format_cost(task_result['total_cost'])}</td>
      </tr>"""
        )
    if not rows:
        rows.append('      <tr><td colspan="4">No results available</td></tr>')

    display_name = task_display_name(task_ref, task_definitions)
    description = task_description(task_ref, task_definitions)
    pass_rate = (passed_agents / total_agents * 100.0) if total_agents else 0.0
    timestamp = datetime.now().strftime("%I:%M %p, %-m/%-d/%Y").lstrip("0")
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>{escape(display_name)} - abc-ci</title>
  <link rel=\"stylesheet\" href=\"../style.css\">
</head>
<body>
  <div class=\"page\">
    <section class=\"report-frame\">
      <div class=\"report-nav\">
        <div class=\"breadcrumbs\">
          <a href=\"../index.html\">Leaderboard</a>
          <span>&gt;</span>
          <span>{escape(display_name)}</span>
        </div>
        <a href=\"../index.html\">Back to Leaderboard</a>
      </div>

      <h1>{escape(display_name)}</h1>
      <p class=\"metadata\">{escape(description)}</p>

      <table class=\"summary-table\">
        <tbody>
          <tr><th>Task Ref</th><td>{escape(task_ref)}</td></tr>
          <tr><th>Overall Pass Rate</th><td>{pass_rate:.0f}%</td></tr>
          <tr><th>Agents Passed</th><td>{passed_agents} / {total_agents}</td></tr>
        </tbody>
      </table>

      <h2>Agent Results</h2>
      <div class=\"table-scroll\">
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Agent ID</th>
              <th class=\"numeric\">Status</th>
              <th class=\"numeric\">Cost</th>
            </tr>
          </thead>
          <tbody>
{''.join(rows)}
          </tbody>
        </table>
      </div>

      <p class=\"table-note\">Last Updated: {timestamp}</p>
    </section>
  </div>
</body>
</html>"""

    task_output_dir = output_dir / "tasks"
    task_output_dir.mkdir(parents=True, exist_ok=True)
    (task_output_dir / _task_filename(task_ref)).write_text(html, encoding="utf-8")


def _agent_summary_rows(agents: list[dict[str, Any]], model_definitions: dict[str, Any], harness_definitions: dict[str, Any] | None = None) -> str:
    harness_definitions = harness_definitions or {}
    rows = []
    for agent_data in sorted(agents, key=lambda a: (-a["pass_rate"], -a["completeness"], a["agent"])):
        models_text = ", ".join(
            model_display_name(m, model_definitions) for m in agent_data["models"]
        ) or "n/a"
        label = agent_label(agent_data["harness"], agent_data["models"], harness_definitions, model_definitions)
        rows.append(
            f"""      <tr>
        <td><a href=\"../agents/{_agent_filename(agent_data['agent'])}\">{escape(label)}</a></td>
        <td>{escape(harness_display_name(agent_data['harness'], harness_definitions))}</td>
        <td>{escape(models_text)}</td>
        <td class=\"numeric\">{agent_data['pass_rate']:.0f}%</td>
        <td class=\"numeric\">{agent_data['completeness']:.0f}%</td>
        <td class=\"numeric\">{agent_data['passed_count']} / {agent_data['attempt_count']}</td>
      </tr>"""
        )
    if not rows:
        rows.append('      <tr><td colspan="6">No results available</td></tr>')
    return "".join(rows)


def generate_model_detail_page(
    model_id: str,
    view: dict[str, Any],
    output_dir: Path,
    model_definitions: dict[str, Any],
    harness_definitions: dict[str, Any],
) -> None:
    display_name = model_display_name(model_id, model_definitions)
    description = model_description(model_id, model_definitions)
    provider = model_provider(model_id, model_definitions)
    logo = model_logo(model_id, model_definitions)
    logo_html = _logo_img(logo, display_name, css_class="detail-logo", prefix="../")

    harness_links = ", ".join(
        f'<a href="../harnesses/{_harness_filename(h)}">{escape(harness_display_name(h, harness_definitions))}</a>'
        for h in sorted(view["harnesses"])
    ) or "n/a"

    agent_rows = _agent_summary_rows(view["agents"], model_definitions, harness_definitions)
    timestamp = datetime.now().strftime("%I:%M %p, %-m/%-d/%Y").lstrip("0")
    provider_row = f"<tr><th>Provider</th><td>{escape(provider)}</td></tr>" if provider else ""
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>{escape(display_name)} - abc-ci</title>
  <link rel=\"stylesheet\" href=\"../style.css\">
</head>
<body>
  <div class=\"page\">
    <section class=\"report-frame\">
      <div class=\"report-nav\">
        <div class=\"breadcrumbs\">
          <a href=\"../index.html\">Leaderboard</a>
          <span>&gt;</span>
          <a href=\"../models.html\">Models</a>
          <span>&gt;</span>
          <span>{escape(display_name)}</span>
        </div>
        <a href=\"../index.html\">Back to Leaderboard</a>
      </div>

      <h1 class=\"detail-title\">{logo_html}<span>{escape(display_name)}</span></h1>
      <p class=\"metadata\">{escape(description)}</p>

      <table class=\"summary-table\">
        <tbody>
          <tr><th>Model ID</th><td>{escape(model_id)}</td></tr>
          {provider_row}
          <tr><th>Harnesses</th><td>{harness_links}</td></tr>
          <tr><th>Configurations</th><td>{view['agent_count']}</td></tr>
          <tr><th>Best Pass Rate</th><td>{view['best_pass_rate']:.0f}%</td></tr>
          <tr><th>Average Pass Rate</th><td>{view['avg_pass_rate']:.0f}%</td></tr>
        </tbody>
      </table>

      <h2>Configurations Using This Model</h2>
      <div class=\"table-scroll\">
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Harness</th>
              <th>Models</th>
              <th class=\"numeric\">Pass Rate</th>
              <th class=\"numeric\">Completeness</th>
              <th class=\"numeric\">Passed</th>
            </tr>
          </thead>
          <tbody>
{agent_rows}
          </tbody>
        </table>
      </div>

      <p class=\"table-note\">Last Updated: {timestamp}</p>
    </section>
  </div>
</body>
</html>"""

    model_output_dir = output_dir / "models"
    model_output_dir.mkdir(parents=True, exist_ok=True)
    (model_output_dir / _model_filename(model_id)).write_text(html, encoding="utf-8")


def generate_harness_detail_page(
    harness_id: str,
    view: dict[str, Any],
    output_dir: Path,
    harness_definitions: dict[str, Any],
    model_definitions: dict[str, Any],
) -> None:
    display_name = harness_display_name(harness_id, harness_definitions)
    description = harness_description(harness_id, harness_definitions)

    model_links = ", ".join(
        f'<a href="../models/{_model_filename(m)}">{escape(model_display_name(m, model_definitions))}</a>'
        for m in sorted(view["models"])
    ) or "n/a"

    agent_rows = _agent_summary_rows(view["agents"], model_definitions, harness_definitions)
    timestamp = datetime.now().strftime("%I:%M %p, %-m/%-d/%Y").lstrip("0")
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>{escape(display_name)} - abc-ci</title>
  <link rel=\"stylesheet\" href=\"../style.css\">
</head>
<body>
  <div class=\"page\">
    <section class=\"report-frame\">
      <div class=\"report-nav\">
        <div class=\"breadcrumbs\">
          <a href=\"../index.html\">Leaderboard</a>
          <span>&gt;</span>
          <span>{escape(display_name)}</span>
        </div>
        <a href=\"../index.html\">Back to Leaderboard</a>
      </div>

      <h1>{escape(display_name)}</h1>
      <p class=\"metadata\">{escape(description)}</p>

      <table class=\"summary-table\">
        <tbody>
          <tr><th>Harness ID</th><td>{escape(harness_id)}</td></tr>
          <tr><th>Models Used</th><td>{model_links}</td></tr>
          <tr><th>Configurations</th><td>{view['agent_count']}</td></tr>
          <tr><th>Best Pass Rate</th><td>{view['best_pass_rate']:.0f}%</td></tr>
          <tr><th>Average Pass Rate</th><td>{view['avg_pass_rate']:.0f}%</td></tr>
        </tbody>
      </table>

      <h2>Configurations Using This Harness</h2>
      <div class=\"table-scroll\">
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Harness</th>
              <th>Models</th>
              <th class=\"numeric\">Pass Rate</th>
              <th class=\"numeric\">Completeness</th>
              <th class=\"numeric\">Passed</th>
            </tr>
          </thead>
          <tbody>
{agent_rows}
          </tbody>
        </table>
      </div>

      <p class=\"table-note\">Last Updated: {timestamp}</p>
    </section>
  </div>
</body>
</html>"""

    harness_output_dir = output_dir / "harnesses"
    harness_output_dir.mkdir(parents=True, exist_ok=True)
    (harness_output_dir / _harness_filename(harness_id)).write_text(html, encoding="utf-8")


def generate_models_index_page(
    model_views: dict[str, dict[str, Any]],
    output_dir: Path,
    model_definitions: dict[str, Any],
) -> None:
    rows = []
    for model_id, view in sorted(
        model_views.items(), key=lambda item: (-item[1]["best_pass_rate"], item[0])
    ):
        display_name = model_display_name(model_id, model_definitions)
        provider = model_provider(model_id, model_definitions)
        logo = model_logo(model_id, model_definitions)
        logo_html = _logo_img(logo, display_name, css_class="row-logo")
        rows.append(
            f"""      <tr>
        <td class=\"model-name-cell\">{logo_html}<a href=\"models/{_model_filename(model_id)}\">{escape(display_name)}</a></td>
        <td>{escape(provider)}</td>
        <td class=\"numeric\">{view['best_pass_rate']:.0f}%</td>
        <td class=\"numeric\">{view['avg_pass_rate']:.0f}%</td>
        <td class=\"numeric\">{view['agent_count']}</td>
      </tr>"""
        )
    if not rows:
        rows.append('      <tr><td colspan="5">No models recorded</td></tr>')

    timestamp = datetime.now().strftime("%I:%M %p, %-m/%-d/%Y").lstrip("0")
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>Models - abc-ci</title>
  <link rel=\"stylesheet\" href=\"style.css\">
</head>
<body>
  <div class=\"page\">
    <section class=\"report-frame\">
      <header class=\"site-header\">
        <h1>Models</h1>
        <nav class=\"header-nav\">
          <a href=\"index.html\">Leaderboard</a>
          <a href=\"docs/\">Documentation</a>
        </nav>
      </header>

      <p class=\"metadata\">Models evaluated across benchmark configurations. Click a model for its report.</p>

      <div class=\"table-scroll\">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th>Provider</th>
              <th class=\"numeric\">Best Pass Rate</th>
              <th class=\"numeric\">Avg Pass Rate</th>
              <th class=\"numeric\">Configurations</th>
            </tr>
          </thead>
          <tbody>
{''.join(rows)}
          </tbody>
        </table>
      </div>

      <p class=\"table-note\">Last Updated: {timestamp}</p>
    </section>
  </div>
</body>
</html>"""
    (output_dir / "models.html").write_text(html, encoding="utf-8")


def generate_leaderboard_html(
    aggregated: dict[str, Any],
    task_definitions: dict[str, Any],
    output_file: Path,
    model_definitions: dict[str, Any] | None = None,
    harness_definitions: dict[str, Any] | None = None,
    static_dir: Path | None = None,
) -> None:
    model_definitions = model_definitions or {}
    harness_definitions = harness_definitions or {}
    if static_dir is None:
        static_dir = Path(__file__).parent / "static"
    agents = sorted(
        aggregated["agents"].items(),
        key=lambda item: (-item[1]["pass_rate"], -item[1]["completeness"], item[0]),
    )
    all_tasks = aggregated["all_tasks"]
    task_groups = _group_tasks(all_tasks, task_definitions)
    group_names = [g["name"] for g in task_groups]

    harness_options = sorted({data["harness"] for _, data in agents if data["harness"]})
    model_options = sorted({model for _, data in agents for model in data["models"] if model})

    header_cells = []
    for group in task_groups:
        group_name = group["name"]
        header_cells.append(
            f'<th class="numeric group-header" data-group-name="{escape(group_name)}">'
            f'<button type="button" class="sort-button sort-button-full group-sort-btn" '
            f'aria-label="Sort by {escape(group_name)}" data-sort-label="Sort by {escape(group_name)}" title="Click to sort, double-click to expand/collapse">'
            f'<span class="sort-label">{escape(group_name)}</span>'
            f'<span class="sort-icon" aria-hidden="true"></span>'
            f'</button>'
            f'</th>'
        )
        for task_ref in group["tasks"]:
            task_name = escape(task_display_name(task_ref, task_definitions))
            header_cells.append(
                f'<th class="numeric task-header hidden" data-group-name="{escape(group_name)}">'
                f'<button type="button" class="sort-button sort-button-full task-sort-btn" '
                f'aria-label="Sort by {task_name}" data-sort-label="Sort by {task_name}">'
                f'<span class="task-sort-label"><a href="tasks/{_task_filename(task_ref)}">{task_name}</a></span>'
                f'<span class="sort-icon" aria-hidden="true"></span>'
                f'</button>'
                f'</th>'
            )

    table_rows = []
    for agent_name, agent_data in agents:
        models_text = ", ".join(agent_data["models"]) or "n/a"
        harness_id = agent_data["harness"]
        harness_label = harness_display_name(harness_id, harness_definitions)
        harness_cell_html = (
            f'<a href="harnesses/{_harness_filename(harness_id)}">{escape(harness_label)}</a>'
            if harness_id else escape(harness_label)
        )
        model_parts = []
        for m in agent_data["models"]:
            label = model_display_name(m, model_definitions)
            logo_html = _logo_img(model_logo(m, model_definitions), label, css_class="row-logo")
            model_parts.append(
                f'<span class="model-chip">{logo_html}'
                f'<a href="models/{_model_filename(m)}">{escape(label)}</a></span>'
            )
        models_display_html = ", ".join(model_parts) or "n/a"
        pass_rate_ci = agent_data["pass_rate_ci"]
        average_cost_text = _format_cost(agent_data["average_cost"])
        average_walltime_text = _format_duration(agent_data["average_walltime"])
        average_cost_val = agent_data["average_cost"]
        average_walltime_val = agent_data["average_walltime"]
        average_tokens_val = agent_data["average_tokens"]
        average_tps_val = agent_data["average_tokens_per_second"]
        pass_rate_cell = _stat_span(
            f"{agent_data['pass_rate']:.0f}%",
            _format_percentage_ci(pass_rate_ci) if pass_rate_ci else "",
        )
        tokens_cell = _stat_span(
            _format_tokens(average_tokens_val),
            _format_ci_generic(agent_data["tokens_ci"], _format_tokens),
        )
        tps_cell = _stat_span(
            _format_tokens_per_second(average_tps_val),
            _format_ci_generic(agent_data["tokens_per_second_ci"], _format_tokens_per_second),
        )
        cost_cell = _stat_span(average_cost_text, _format_ci_generic(agent_data["cost_ci"], _format_cost))
        walltime_cell = _stat_span(average_walltime_text, _format_ci_generic(agent_data["walltime_ci"], _format_duration))

        row_cells = []
        for group in task_groups:
            group_tasks = group["tasks"]
            results = [agent_data["tasks"].get(tr) for tr in group_tasks]
            ran = [r for r in results if r is not None]
            passed = sum(1 for r in ran if r["passed"])

            group_pass_rate = (passed / len(ran) * 100.0) if ran else 0.0
            group_status_class = "status-pass" if group_pass_rate == 100 else "status-fail" if group_pass_rate == 0 else "status-muted"
            if not ran: group_status_class = "status-muted"

            row_cells.append(
                f'<td class="numeric group-cell {group_status_class}" data-col="group" data-group-summary="{group_pass_rate:.0f}%" data-group-name="{escape(group["name"])}">'
                f'{group_pass_rate:.0f}%'
                f'</td>'
            )

            for task_ref in group_tasks:
                task_result = agent_data["tasks"].get(task_ref)
                group_name_esc = escape(group["name"])
                if task_result is None:
                    cell = f'<td class="numeric task-col hidden status-muted" data-group-name="{group_name_esc}">-</td>'
                elif task_result["attempts"] > 1:
                    rate = task_result["pass_rate"]
                    status_class = "status-pass" if rate == 100 else "status-fail" if rate == 0 else "status-muted"
                    value = _stat_span(
                        f"{rate:.0f}%",
                        f'{task_result["passed_count"]}/{task_result["attempts"]} attempts passed',
                    )
                    cell = f'<td class="numeric task-col hidden {status_class}" data-group-name="{group_name_esc}">{value}</td>'
                elif task_result["status"] == "PASS":
                    cell = f'<td class="numeric task-col hidden status-pass" data-group-name="{group_name_esc}">Pass</td>'
                elif task_result["status"] == "FAIL":
                    cell = f'<td class="numeric task-col hidden status-fail" data-group-name="{group_name_esc}">Fail</td>'
                else:
                    cell = f'<td class="numeric task-col hidden status-muted" data-group-name="{group_name_esc}">{escape(task_result["status"])}</td>'
                row_cells.append(cell)

        row_pass_rate = agent_data["pass_rate"]
        row_completeness = agent_data["completeness"]
        table_rows.append(
            f"""      <tr class="agent-row"
        data-agent=\"{escape(agent_name)}\"
        data-harness=\"{escape(agent_data['harness'])}\"
        data-models=\"{escape(models_text)}\"
        data-pass-rate=\"{row_pass_rate:.6f}\"
        data-completeness=\"{row_completeness:.6f}\">
        <td data-sort-value=\"{escape(agent_data['harness'])}\">{harness_cell_html}</td>
        <td data-sort-value=\"{escape(models_text)}\">{models_display_html}</td>
        <td class=\"numeric\" data-col=\"pass_rate\" data-sort-value=\"{agent_data['pass_rate']:.6f}\">{pass_rate_cell}</td>
        <td class=\"numeric\" data-col=\"tokens\" data-sort-value=\"{'' if average_tokens_val is None else f'{average_tokens_val:.6f}'}\">{tokens_cell}</td>
        <td class=\"numeric\" data-col=\"tps\" data-sort-value=\"{'' if average_tps_val is None else f'{average_tps_val:.6f}'}\">{tps_cell}</td>
        <td class=\"numeric\" data-col=\"completeness\" data-sort-value=\"{agent_data['completeness']:.6f}\">{agent_data['completeness']:.0f}%</td>
        <td class=\"numeric\" data-col=\"cost\" data-sort-value=\"{'' if average_cost_val is None else f'{average_cost_val:.6f}'}\">{cost_cell}</td>
        <td class=\"numeric\" data-col=\"walltime\" data-sort-value=\"{'' if average_walltime_val is None else f'{average_walltime_val:.6f}'}\">{walltime_cell}</td>
        {''.join(row_cells)}
      </tr>"""
        )

    timestamp = datetime.now().strftime("%I:%M %p, %-m/%-d/%Y").lstrip("0")

    harness_option_html = "".join(
        f'<option value="{escape(h)}">{escape(h)}</option>' for h in harness_options
    )
    model_option_html = "".join(
        f'<option value="{escape(m)}">{escape(model_display_name(m, model_definitions))}</option>' for m in model_options
    )
    filter_bar_html = f"""      <div class="filters" id="filter-bar">
        <div class="filter-field">
          <label for="filter-search">Search</label>
          <input type="search" id="filter-search" placeholder="harness or model...">
        </div>
        <div class="filter-field">
          <label for="filter-harness">Harness</label>
          <select id="filter-harness">
            <option value="">All harnesses</option>
            {harness_option_html}
          </select>
        </div>
        <div class="filter-field">
          <label for="filter-model">Model</label>
          <select id="filter-model">
            <option value="">All models</option>
            {model_option_html}
          </select>
        </div>
        <div class="filter-field">
          <button type="button" id="filter-reset">Reset</button>
        </div>
        <p class="filter-status" id="filter-status" aria-live="polite"></p>
      </div>
"""

    group_checkbox_html = "".join(
        f'<label class="group-toggle">'
        f'<input type="checkbox" class="group-checkbox" value="{escape(name)}" checked> '
        f'<span>{escape(name)}</span>'
        f'</label>'
        for name in group_names
    )
    group_selector_html = f"""      <fieldset class="group-selector" id="group-selector">
        <legend>Task groups</legend>
        <p class="group-selector-hint">Unchecked groups are excluded from every statistic below.</p>
        <div class="group-selector-options">
          {group_checkbox_html}
        </div>
        <div class="group-selector-actions">
          <button type="button" id="group-select-all">Select all</button>
          <button type="button" id="group-select-none">Select none</button>
        </div>
      </fieldset>
""" if group_names else ""

    plot_data = []
    for agent_name, agent_data in agents:
        ci = agent_data["pass_rate_ci"]
        logos = []
        for m in agent_data["models"]:
            src = model_logo(m, model_definitions)
            if not src:
                continue
            analysis = analyze_logo(src, static_dir)
            logos.append(
                {
                    "src": src,
                    "inset_left": round(analysis["inset_left"], 4),
                    "inset_right": round(analysis["inset_right"], 4),
                    "cx": round(analysis["cx"], 4),
                }
            )
        plot_data.append(
            {
                "name": agent_name,
                "harness": agent_data["harness"],
                "models": ", ".join(agent_data["models"]) or "n/a",
                "label": agent_label(
                    agent_data["harness"],
                    agent_data["models"],
                    harness_definitions,
                    model_definitions,
                ),
                "logos": logos,
                "pass_rate": round(agent_data["pass_rate"], 4),
                "ci_low": round(ci[0], 4) if ci else None,
                "ci_high": round(ci[1], 4) if ci else None,
                "completeness": round(agent_data["completeness"], 4),
                "href": f"agents/{_agent_filename(agent_name)}",
                # Raw per-group counts/samples so the browser can recompute every
                # statistic from only the selected task groups (recompute.js).
                "groups": {
                    name: {
                        "attempt_count": gb["attempt_count"],
                        "passed_count": gb["passed_count"],
                        "ran_count": gb["ran_count"],
                        "costs": [round(c, 6) for c in gb["costs"]],
                        "durations": [round(d, 6) for d in gb["durations"]],
                        "tokens": [round(t, 6) for t in gb["tokens"]],
                        "tps": [round(t, 6) for t in gb["tps"]],
                    }
                    for name, gb in agent_data["group_breakdown"].items()
                },
            }
        )
    agent_data_json = json.dumps(plot_data)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ABC-Bench Leaderboard</title>
  <link rel="stylesheet" href="style.css">
  <script defer src="sort.js"></script>
  <script defer src="groups.js"></script>
  <script defer src="filters.js"></script>
  <script defer src="plot.js"></script>
  <script defer src="viz.js"></script>
  <script defer src="recompute.js"></script>
</head>
<body>
  <div class="page">
    <section class="report-frame">
      <header class="site-header">
        <h1>ABC-Bench Leaderboard</h1>
        <nav class="header-nav">
          <a href="models.html">Models</a>
          <a href="docs/">Documentation</a>
        </nav>
      </header>

      <p class="metadata">Practical first-pass view of benchmark results by harness and model configuration.</p>

      <fieldset class="viz-selector" id="viz-selector">
        <legend>Visualization</legend>
        <label><input type="radio" name="viz" value="table" checked> Table</label>
        <label><input type="radio" name="viz" value="plot"> Plot</label>
      </fieldset>

{group_selector_html}
      <script id="agent-data" type="application/json">{agent_data_json}</script>

      <section class="viz-view" id="view-table">
{filter_bar_html}
        <div class="table-scroll">
          <table id="leaderboard-table">
            <thead>
              <tr id="header-row">
                {_sortable_header('Harness', 'text', button_label='Sort by harness')}
                {_sortable_header('Models', 'text', button_label='Sort by models')}
                {_sortable_header('Pass<br>Rate', 'number', th_class='numeric', default_sort='descending', button_label='Sort by pass rate')}
                {_sortable_header('Avg<br>Tokens', 'number', th_class='numeric', button_label='Sort by average tokens')}
                {_sortable_header('Avg<br>Tok/s', 'number', th_class='numeric', button_label='Sort by average tokens per second')}
                {_sortable_header('Completeness', 'number', th_class='numeric', button_label='Sort by completeness')}
                {_sortable_header('Avg<br>Cost', 'number', th_class='numeric', button_label='Sort by average cost')}
                {_sortable_header('Avg<br>Walltime', 'number', th_class='numeric', button_label='Sort by average walltime')}
                {''.join(header_cells)}
              </tr>
            </thead>
            <tbody>
{''.join(table_rows)}
            </tbody>
          </table>
        </div>
      </section>

      <section class="viz-view hidden" id="view-plot">
        <div class="plot-controls">
          <label for="plot-order">Order by</label>
          <select id="plot-order">
            <option value="pass_rate">Pass rate</option>
            <option value="name">Agent name</option>
          </select>
        </div>
        <div id="plot-container"></div>
      </section>

      <p class="footnote"><sup>1</sup> Pass-rate intervals (shown on hover) are 95% Wilson score intervals for a binomial proportion; other averages use a 95% normal-approximation interval. All statistics recompute from the selected task groups only.</p>
      <p class="table-note">Last Updated: {timestamp}</p>
    </section>
  </div>
</body>
</html>"""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html, encoding="utf-8")


def copy_static_assets(static_dir: Path, output_dir: Path) -> None:
    if not static_dir.exists():
        return
    for item in static_dir.rglob("*"):
        if item.is_file():
            dest = output_dir / item.relative_to(static_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(item.read_bytes())


def main() -> int:
    script_dir = Path(__file__).parent
    repo_root = script_dir.parent
    archive_dir = Path(os.environ.get("ABC_BENCH_ARCHIVE_DIR", repo_root / "test_archive"))
    output_dir = Path(os.environ.get("SITE_OUTPUT_DIR", repo_root / "public"))
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building site from archive: {archive_dir}")
    print(f"Output directory: {output_dir}")

    runs = find_runs(archive_dir)
    print(f"Found {len(runs)} archived run(s)")

    task_definitions = load_task_definitions(script_dir / "tasks")
    model_definitions = load_model_definitions(script_dir / "models")
    harness_definitions = load_harness_definitions(script_dir / "harnesses")

    aggregated = aggregate_runs(runs, task_definitions)
    dimensions = _dimension_views(aggregated)

    for agent_name, agent_data in aggregated["agents"].items():
        generate_agent_detail_page(agent_name, agent_data, aggregated["all_tasks"], output_dir, task_definitions, model_definitions, harness_definitions)

    for task_ref in aggregated["all_tasks"]:
        generate_task_detail_page(task_ref, aggregated, task_definitions, output_dir, model_definitions, harness_definitions)

    for model_id, view in dimensions["models"].items():
        generate_model_detail_page(model_id, view, output_dir, model_definitions, harness_definitions)

    for harness_id, view in dimensions["harnesses"].items():
        generate_harness_detail_page(harness_id, view, output_dir, harness_definitions, model_definitions)

    generate_models_index_page(dimensions["models"], output_dir, model_definitions)

    generate_leaderboard_html(
        aggregated,
        task_definitions,
        output_dir / "index.html",
        model_definitions,
        harness_definitions,
        script_dir / "static",
    )
    copy_static_assets(script_dir / "static", output_dir)

    print("Site build complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
