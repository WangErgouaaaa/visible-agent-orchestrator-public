"""Deterministic task-feature profile and conservative topology policy."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
POLICY_VERSION = 1
DEPENDENCY_SHAPES = {"independent", "mixed", "sequential"}
LEVELS = {"low", "medium", "high"}
_REQUIRED_FIELDS = (
    "schema_version",
    "profile_id",
    "independent_work_packages",
    "dependency_shape",
    "tool_intensity",
    "context_pressure",
    "budget_level",
)


class TaskFeatureTopologyPolicyError(Exception):
    """Operator-facing task profile validation error."""


def resolve_task_feature_profile(path_value: str | Path | None) -> dict[str, Any]:
    """Read and normalize one optional task-feature JSON profile."""
    path_text = str(path_value or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise TaskFeatureTopologyPolicyError(f"task profile file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFeatureTopologyPolicyError(
            f"task profile must be valid JSON: {path}"
        ) from exc
    normalized = _normalize_profile(payload)
    normalized["source_path"] = str(path)
    return normalized


def recommend_topology(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return a conservative outer-worker fanout recommendation."""
    normalized = _normalize_profile(profile)
    packages = normalized["independent_work_packages"]
    dependency = normalized["dependency_shape"]
    tool_intensity = normalized["tool_intensity"]
    budget = normalized["budget_level"]

    if dependency == "sequential":
        fanout, confidence, reasons = 1, "high", ["sequential_dependency"]
    elif tool_intensity == "high":
        fanout, confidence, reasons = 1, "high", ["tool_coordination_penalty"]
    elif budget == "low":
        fanout, confidence, reasons = 1, "high", ["low_budget"]
    elif (
        dependency == "independent"
        and packages >= 3
        and normalized["context_pressure"] in {"medium", "high"}
    ):
        fanout, confidence, reasons = 3, "high", [
            "three_or_more_independent_work_packages",
            "budget_allows_bounded_parallelism",
        ]
    elif packages >= 2:
        fanout, confidence, reasons = 2, "medium", [
            "two_or_more_separable_work_packages"
        ]
    else:
        fanout, confidence, reasons = 1, "medium", [
            "single_or_unproven_work_package"
        ]

    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "source": "explicit_task_profile",
        "recommended_fanout": fanout,
        "confidence": confidence,
        "reasons": reasons,
    }


def _normalize_profile(profile: Mapping[str, Any] | object) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise TaskFeatureTopologyPolicyError("task profile must be a JSON object")
    for field in _REQUIRED_FIELDS:
        if field not in profile:
            raise TaskFeatureTopologyPolicyError(
                f"task profile missing required field: {field}"
            )

    schema_version = profile["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise TaskFeatureTopologyPolicyError(
            f"task profile schema_version must be {SCHEMA_VERSION}"
        )

    profile_id = profile["profile_id"]
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise TaskFeatureTopologyPolicyError("task profile profile_id must be non-empty")

    packages = profile["independent_work_packages"]
    if type(packages) is not int or not 1 <= packages <= 7:
        raise TaskFeatureTopologyPolicyError(
            "task profile independent_work_packages must be an integer from 1 to 7"
        )

    dependency_shape = _normalize_level(
        profile["dependency_shape"],
        "dependency_shape",
        DEPENDENCY_SHAPES,
    )
    tool_intensity = _normalize_level(
        profile["tool_intensity"],
        "tool_intensity",
        LEVELS,
    )
    context_pressure = _normalize_level(
        profile["context_pressure"],
        "context_pressure",
        LEVELS,
    )
    budget_level = _normalize_level(
        profile["budget_level"],
        "budget_level",
        LEVELS,
    )
    return {
        "schema_version": schema_version,
        "profile_id": profile_id.strip(),
        "independent_work_packages": packages,
        "dependency_shape": dependency_shape,
        "tool_intensity": tool_intensity,
        "context_pressure": context_pressure,
        "budget_level": budget_level,
    }


def _normalize_level(value: object, field: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise TaskFeatureTopologyPolicyError(f"task profile {field} is invalid")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise TaskFeatureTopologyPolicyError(f"task profile {field} is invalid")
    return normalized
