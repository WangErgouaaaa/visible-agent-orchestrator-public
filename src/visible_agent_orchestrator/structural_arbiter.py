from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


SCHEMA_VERSION = 1
ARBITER_KIND = "arbiter_decision"
MIN_STRUCTURAL_CONFIDENCE = 0.7


def build_arbiter_decision(findings: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [_normalize_finding(item) for item in findings if isinstance(item, dict)]
    high_confidence = [
        item
        for item in normalized
        if item["evidence_key"] and float(item["confidence"]) >= MIN_STRUCTURAL_CONFIDENCE
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in high_confidence:
        grouped[item["evidence_key"]].append(item)

    for evidence_key in sorted(grouped):
        group = grouped[evidence_key]
        if len(group) < 2 or not _has_structural_conflict(group):
            continue
        finding_ids = [item["finding_id"] for item in sorted(group, key=lambda row: row["finding_id"])]
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": ARBITER_KIND,
            "triggered": True,
            "conflict_id": "conflict-" + _safe_id(evidence_key),
            "input_finding_ids": finding_ids,
            "decision": "inconclusive",
            "recommended_leader_action": "human_review_required",
            "reason": "Structured findings conflict over the same evidence key.",
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": ARBITER_KIND,
        "triggered": False,
        "conflict_id": "",
        "input_finding_ids": [],
        "decision": "no_structural_conflict",
        "recommended_leader_action": "none",
        "reason": "No high-confidence structural conflict found.",
    }


def _normalize_finding(item: dict[str, Any]) -> dict[str, Any]:
    target_paths = item.get("target_paths") if isinstance(item.get("target_paths"), list) else []
    evidence_key = (
        str(item.get("evidence_label") or "").strip()
        or str(item.get("decision_point") or "").strip()
        or (str(target_paths[0]).strip() if target_paths else "")
    )
    return {
        "finding_id": str(item.get("finding_id") or "").strip(),
        "evidence_key": evidence_key,
        "severity": str(item.get("severity") or "").strip().lower(),
        "confidence": _confidence(item.get("confidence")),
        "recommended_action": str(item.get("recommended_action") or "").strip(),
        "summary": str(item.get("summary") or "").strip(),
    }


def _has_structural_conflict(group: list[dict[str, Any]]) -> bool:
    actions = {str(item.get("recommended_action") or "") for item in group}
    severities = {str(item.get("severity") or "") for item in group}
    if "block" in actions and "accept" in actions:
        return True
    if "critical" in severities and "info" in severities:
        return True
    return False


def _confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return safe or "unknown"
