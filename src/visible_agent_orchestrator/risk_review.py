from __future__ import annotations

from typing import Any


SCHEMA_VERSION = 1
RISK_REVIEW_KIND = "risk_review"
VALID_SEVERITIES = {"info", "warning", "critical"}
VALID_RECOMMENDED_ACTIONS = {
    "accept",
    "rerun_lane",
    "block",
    "human_review_required",
}
BLOCKING_CONFIDENCE_THRESHOLD = 0.7


class RiskReviewError(Exception):
    """Raised when a structured risk review artifact is malformed."""


def normalize_risk_review(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RiskReviewError("risk_review_must_be_object")
    schema_version = int(payload.get("schema_version", SCHEMA_VERSION) or 0)
    if schema_version != SCHEMA_VERSION:
        raise RiskReviewError(f"unsupported_risk_review_schema_version:{schema_version}")
    artifact_kind = str(payload.get("artifact_kind") or RISK_REVIEW_KIND).strip()
    if artifact_kind != RISK_REVIEW_KIND:
        raise RiskReviewError(f"unsupported_risk_review_kind:{artifact_kind}")
    reviewer_id = _required_str(payload.get("reviewer_id"), "reviewer_id")
    findings_raw = payload.get("findings")
    if not isinstance(findings_raw, list):
        raise RiskReviewError("risk_review findings must be an array")
    findings = [_normalize_finding(item, index=index) for index, item in enumerate(findings_raw)]
    critical = [item for item in findings if item["severity"] == "critical"]
    warnings = [item for item in findings if item["severity"] == "warning"]
    blocking = [
        item["finding_id"]
        for item in critical
        if float(item["confidence"]) >= BLOCKING_CONFIDENCE_THRESHOLD
    ]
    low_confidence = [
        item["finding_id"]
        for item in critical
        if float(item["confidence"]) < BLOCKING_CONFIDENCE_THRESHOLD
    ]
    if blocking:
        status = "critical"
    elif low_confidence:
        status = "low_confidence_critical"
    elif warnings:
        status = "warning"
    elif findings:
        status = "info"
    else:
        status = "clear"
    return {
        "schema_version": schema_version,
        "artifact_kind": artifact_kind,
        "reviewer_id": reviewer_id,
        "status": status,
        "finding_count": len(findings),
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "blocking_findings": blocking,
        "low_confidence_findings": low_confidence,
        "findings": findings,
    }


def _normalize_finding(item: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise RiskReviewError(f"risk_review findings[{index}] must be an object")
    finding_id = _required_str(item.get("finding_id"), f"findings[{index}].finding_id")
    severity = _required_str(item.get("severity"), f"findings[{index}].severity").lower()
    if severity not in VALID_SEVERITIES:
        raise RiskReviewError(f"unknown_risk_severity:{severity}")
    confidence = _confidence(item.get("confidence"))
    recommended_action = str(item.get("recommended_action") or "").strip()
    if recommended_action and recommended_action not in VALID_RECOMMENDED_ACTIONS:
        raise RiskReviewError(f"unknown_risk_recommended_action:{recommended_action}")
    target_paths = _string_list(item.get("target_paths"))
    return {
        "finding_id": finding_id,
        "severity": severity,
        "confidence": confidence,
        "evidence_label": str(item.get("evidence_label") or "").strip(),
        "affected_evidence_domain": str(item.get("affected_evidence_domain") or "").strip(),
        "target_paths": target_paths,
        "summary": _required_str(item.get("summary"), f"findings[{index}].summary"),
        "recommended_action": recommended_action,
    }


def _required_str(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RiskReviewError(f"missing_risk_review_{field}")
    return text


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskReviewError("risk_review confidence must be a 0-1 number") from exc
    if confidence < 0 or confidence > 1:
        raise RiskReviewError("risk_review confidence must be a 0-1 number")
    return confidence


def _string_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise RiskReviewError("risk_review target_paths must be an array")
    return [str(item).strip() for item in value if str(item).strip()]
