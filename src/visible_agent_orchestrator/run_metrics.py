"""Run-level cost telemetry and advisory execution budgets."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
BUDGET_TO_METRIC = {
    "max_total_tokens": "total_tokens",
    "max_duration_ms": "duration_ms",
    "max_tool_calls": "tool_calls",
    "max_retry_count": "retry_count",
    "max_iteration_count": "iteration_count",
}
METRIC_NAMES = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "duration_ms",
    "tool_calls",
    "retry_count",
    "iteration_count",
)


class RunMetricsError(Exception):
    """Operator-facing run metrics or budget validation error."""


def load_execution_budget(path_value: str | Path | None) -> dict[str, Any]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise RunMetricsError(f"execution budget file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RunMetricsError(f"execution budget must be valid JSON: {path}") from exc
    if not isinstance(payload, Mapping):
        raise RunMetricsError("execution budget must be a JSON object")
    budget = normalize_execution_budget(payload)
    budget["source_path"] = str(path)
    return budget


def normalize_execution_budget(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    schema_version = value.get("schema_version", SCHEMA_VERSION)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise RunMetricsError("execution budget schema_version must be 1")
    budget_id = str(value.get("budget_id") or "").strip()
    if not budget_id:
        raise RunMetricsError("execution budget missing budget_id")
    limits = {
        key: _budget_limit(value.get(key), key)
        for key in BUDGET_TO_METRIC
    }
    if all(limit is None for limit in limits.values()):
        raise RunMetricsError("execution budget requires at least one max_* limit")
    return {
        "schema_version": SCHEMA_VERSION,
        "budget_id": budget_id,
        **limits,
    }


def build_run_metrics(
    results: Sequence[Mapping[str, Any]],
    *,
    lane_assignments: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = [dict(item) for item in results if isinstance(item, Mapping)]
    warnings: list[str] = []
    values = {
        metric: [_metric(row, metric, warnings) for row in rows]
        for metric in METRIC_NAMES
    }
    _derive_total_tokens(values)
    _derive_durations(rows, values)

    status_counts = Counter(str(row.get("state") or "unknown") for row in rows)
    totals = {
        metric: _sum_available(metric_values)
        for metric, metric_values in values.items()
    }
    means = {
        metric: _mean_available(values[metric])
        for metric in (
            "total_tokens",
            "duration_ms",
            "tool_calls",
            "retry_count",
            "iteration_count",
        )
    }
    assignments = list(lane_assignments or [])
    if not assignments:
        assignments = [
            row
            for row in rows
            if str(row.get("required_or_optional") or "").strip()
        ]
    coordination = _coordination_summary(rows, assignments)
    availability = {
        f"{metric}_lane_count": sum(value is not None for value in metric_values)
        for metric, metric_values in values.items()
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "run_metrics",
        "lane_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "metric_availability": availability,
        "totals": totals,
        "means": means,
        "coordination": coordination,
        "missing_metrics": sorted(
            metric
            for metric, metric_values in values.items()
            if not any(value is not None for value in metric_values)
        ),
        "warnings": sorted(set(warnings)),
    }


def evaluate_execution_budget(
    run_metrics: Mapping[str, Any],
    budget: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not budget:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "not_configured",
            "additional_followup_allowed": True,
            "reasons": [],
            "terminal_truth_affected": False,
        }

    totals = run_metrics.get("totals") if isinstance(run_metrics.get("totals"), Mapping) else {}
    availability = (
        run_metrics.get("metric_availability")
        if isinstance(run_metrics.get("metric_availability"), Mapping)
        else {}
    )
    lane_count = int(run_metrics.get("lane_count") or 0)
    reasons: list[str] = []
    unavailable = False
    exhausted = False
    for budget_key, metric in BUDGET_TO_METRIC.items():
        limit = budget.get(budget_key)
        if limit is None:
            continue
        observed = totals.get(metric)
        observed_lane_count = availability.get(f"{metric}_lane_count")
        if observed is None or observed_lane_count != lane_count:
            unavailable = True
            reasons.append(f"budget_metric_unavailable:{metric}")
        elif int(observed) > int(limit):
            exhausted = True
            reasons.append(f"budget_exhausted:{metric}:{observed}>{limit}")

    status = "exhausted" if exhausted else "unavailable" if unavailable else "within_budget"
    return {
        "schema_version": SCHEMA_VERSION,
        "budget_id": str(budget.get("budget_id") or ""),
        "status": status,
        "additional_followup_allowed": status == "within_budget",
        "reasons": reasons,
        "terminal_truth_affected": False,
    }


def build_run_control_advice(
    run_metrics: Mapping[str, Any],
    budget_status: Mapping[str, Any],
) -> dict[str, Any]:
    del run_metrics
    status = str(budget_status.get("status") or "not_configured")
    reasons = [str(item) for item in budget_status.get("reasons", [])]
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "run_control_advice",
        "advisory_only": True,
        "terminal_truth_affected": False,
        "additional_followup_allowed": bool(
            budget_status.get("additional_followup_allowed", True)
        ),
        "early_exit_recommended": status in {"exhausted", "unavailable"},
        "reasons": reasons,
        "existing_optional_lane_control_required": True,
    }


def _budget_limit(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunMetricsError(f"{key} must be a non-negative integer")
    if value < 0:
        raise RunMetricsError(f"{key} must be a non-negative integer")
    return value


def _metric(row: Mapping[str, Any], metric: str, warnings: list[str]) -> int | None:
    value = _first_value(row, metric)
    if value is None:
        return None
    parsed = _runtime_nonnegative_int(value)
    if parsed is None:
        worker_id = str(row.get("worker_id") or "unknown")
        warnings.append(f"metric_invalid:{metric}:{worker_id}")
    return parsed


def _first_value(row: Mapping[str, Any], key: str) -> Any:
    if key in row and row[key] is not None:
        return row[key]
    for container_name in ("metrics", "usage"):
        container = row.get(container_name)
        if isinstance(container, Mapping) and container.get(key) is not None:
            return container[key]
    return None


def _runtime_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _derive_total_tokens(values: dict[str, list[int | None]]) -> None:
    for index, total in enumerate(values["total_tokens"]):
        if total is None:
            prompt = values["prompt_tokens"][index]
            completion = values["completion_tokens"][index]
            if prompt is not None and completion is not None:
                values["total_tokens"][index] = prompt + completion


def _derive_durations(
    rows: Sequence[Mapping[str, Any]],
    values: dict[str, list[int | None]],
) -> None:
    for index, duration in enumerate(values["duration_ms"]):
        if duration is not None:
            continue
        derived = _duration_ms_from_timestamps(
            str(rows[index].get("started_at") or ""),
            str(rows[index].get("completed_at") or ""),
        )
        if derived is not None:
            values["duration_ms"][index] = derived


def _duration_ms_from_timestamps(started_at: str, completed_at: str) -> int | None:
    start = _parse_iso8601(started_at)
    end = _parse_iso8601(completed_at)
    if start is None or end is None:
        return None
    delta_ms = int((end - start).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _parse_iso8601(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sum_available(values: Sequence[int | None]) -> int | None:
    available = [value for value in values if value is not None]
    return sum(available) if available else None


def _mean_available(values: Sequence[int | None]) -> float | None:
    available = [value for value in values if value is not None]
    return float(fmean(available)) if available else None


def _coordination_summary(
    rows: Sequence[Mapping[str, Any]],
    assignments: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    required = sum(
        str(item.get("required_or_optional") or "").strip() == "required"
        for item in assignments
    )
    optional = sum(
        str(item.get("required_or_optional") or "").strip() == "optional"
        for item in assignments
    )
    return {
        "required_lane_count": required,
        "optional_lane_count": optional,
        "unique_value_realized": sum(
            str(row.get("value_classification") or "").strip() == "unique_value_realized"
            for row in rows
        ),
        "duplicate_or_overlap": sum(
            str(row.get("value_classification") or "").strip() == "duplicate_or_overlap"
            for row in rows
        ),
        "direct_path_converged_first": sum(
            row.get("direct_path_converged_first") is True
            for row in rows
        ),
    }
