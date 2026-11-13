"""Offline, artifact-first comparison of orchestration topologies."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping

from visible_agent_orchestrator.subagent_projection import (
    COST_VALUE_SUMMARY_KEYS,
    build_value_feedback_summary,
)


SCHEMA_VERSION = 1
VALID_SPLITS = {"train", "holdout"}
VALID_STATUSES = {"completed", "blocked", "failed", "cancelled"}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
_VALUE_KEYS = (
    "consumed_unique_value",
    "confidence_only",
    "duplicate_or_overlap",
    "direct_path_converged_first",
    "retrieval_failure",
    "retrieved_not_consumed",
    "expired_or_cancelled",
    "unknown",
)
_COST_VALUE_METADATA_KEYS = {
    "schema_version",
    "token_cost_available",
    "token_cost_source",
    "duration_ms_available",
    "duration_ms_source",
}
_ALLOWED_COST_VALUE_KEYS = set(COST_VALUE_SUMMARY_KEYS) | _COST_VALUE_METADATA_KEYS


class OrchestrationEvaluationError(Exception):
    """Operator-facing evaluation dataset error."""


def load_evaluation_dataset(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise OrchestrationEvaluationError(f"evaluation dataset not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestrationEvaluationError(
            f"evaluation dataset must be valid JSON: {path}"
        ) from exc
    normalized = _normalize_dataset(payload)
    normalized["source_path"] = str(path)
    return normalized


def build_sealed_audit_manifest(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Bind normalized holdout case IDs and payloads to one manifest."""

    normalized = _normalize_dataset(dataset)
    holdout_cases = _sealed_holdout_cases(normalized)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "orchestration_sealed_audit_manifest",
        "dataset_id": normalized["dataset_id"],
        "baseline_topology": normalized["baseline_topology"],
        "case_ids": [case["case_id"] for case in holdout_cases],
        "case_count": len(holdout_cases),
        "holdout_sha256": _canonical_sha256(holdout_cases),
    }


def validate_sealed_audit_manifest(
    dataset: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a sealed manifest with the currently observed holdout payload."""

    observed = build_sealed_audit_manifest(dataset)
    errors: list[str] = []
    if not isinstance(manifest, Mapping):
        errors.append("sealed_manifest_must_be_mapping")
        manifest = {}
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("sealed_manifest_schema_version_mismatch")
    if manifest.get("artifact_kind") != "orchestration_sealed_audit_manifest":
        errors.append("sealed_manifest_artifact_kind_mismatch")
    if manifest.get("dataset_id") != observed["dataset_id"]:
        errors.append("sealed_manifest_dataset_id_mismatch")
    if manifest.get("baseline_topology") != observed["baseline_topology"]:
        errors.append("sealed_manifest_baseline_topology_mismatch")
    if manifest.get("case_ids") != observed["case_ids"]:
        errors.append("sealed_manifest_case_ids_mismatch")
    if manifest.get("case_count") != observed["case_count"]:
        errors.append("sealed_manifest_case_count_mismatch")
    expected_hash = str(manifest.get("holdout_sha256") or "")
    if expected_hash != observed["holdout_sha256"]:
        errors.append("holdout_sha256_mismatch")
    return {
        "valid": not errors,
        "errors": errors,
        "case_ids": observed["case_ids"],
        "expected_holdout_sha256": expected_hash,
        "observed_holdout_sha256": observed["holdout_sha256"],
    }


def build_orchestration_evaluation_report(
    dataset: Mapping[str, Any],
    *,
    minimum_cases: int = 20,
    minimum_repeats: int = 3,
    sealed_audit_manifest: Mapping[str, Any] | None = None,
    require_sealed_audit: bool = False,
) -> dict[str, Any]:
    if minimum_cases < 1:
        raise OrchestrationEvaluationError("minimum_cases must be positive")
    if minimum_repeats < 1:
        raise OrchestrationEvaluationError("minimum_repeats must be positive")

    normalized = _normalize_dataset(dataset)
    split_reports = {
        split: _build_split_report(
            [case for case in normalized["cases"] if case["split"] == split],
            baseline_topology=normalized["baseline_topology"],
            minimum_cases=minimum_cases,
            minimum_repeats=minimum_repeats,
        )
        for split in ("train", "holdout")
    }
    decisions = _candidate_decisions(
        split_reports["holdout"],
        baseline_topology=normalized["baseline_topology"],
        candidate_topologies=sorted(
            {
                run["topology"]
                for case in normalized["cases"]
                for run in case["runs"]
                if run["topology"] != normalized["baseline_topology"]
            }
        ),
    )
    sealed_audit = _sealed_audit_result(
        normalized,
        manifest=sealed_audit_manifest,
        required=require_sealed_audit,
    )
    if not sealed_audit["valid"] and (
        require_sealed_audit or sealed_audit_manifest is not None
    ):
        for decision in decisions.values():
            reasons = list(decision.get("reasons") or [])
            if "sealed_audit_invalid" not in reasons:
                reasons.append("sealed_audit_invalid")
            decision.update(
                {
                    "status": "insufficient_evidence",
                    "dominates_baseline": False,
                    "reasons": reasons,
                }
            )
    warnings = [
        f"{split}:{warning}"
        for split in ("train", "holdout")
        for warning in split_reports[split]["warnings"]
    ]
    if not split_reports["holdout"]["case_count"]:
        warnings.append("missing_holdout_cases")
    warnings.extend(
        f"sealed_audit:{error}"
        for error in sealed_audit["errors"]
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "orchestration_evaluation",
        "dataset_id": normalized["dataset_id"],
        "baseline_topology": normalized["baseline_topology"],
        "splits": split_reports,
        "candidate_decisions": decisions,
        "sealed_audit": sealed_audit,
        "automatic_promotion": False,
        "warnings": warnings,
    }


def render_orchestration_evaluation_markdown(report: Mapping[str, Any]) -> str:
    """Render a deterministic operator-facing summary."""

    splits = dict(report.get("splits") or {})
    lines = [
        f"# Orchestration Evaluation: {_md(report.get('dataset_id'))}",
        "",
        "Automatic promotion: disabled",
        "",
        "## Evidence Gates",
        "",
        "| Split | Topology | Cases | Required cases | Minimum repeats | Required repeats | Paired cases | Paired repeats | Satisfied |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for split in ("train", "holdout"):
        split_report = dict(splits.get(split) or {})
        for topology, gate in sorted(
            dict(split_report.get("evidence_gates") or {}).items()
        ):
            gate = dict(gate)
            lines.append(
                "| "
                + " | ".join(
                    (
                        split,
                        _md(topology),
                        str(gate.get("case_count", 0)),
                        str(gate.get("minimum_cases_required", 0)),
                        str(gate.get("minimum_repeats_observed", 0)),
                        str(gate.get("minimum_repeats_required", 0)),
                        "yes" if gate.get("paired_case_coverage") else "no",
                        "yes" if gate.get("paired_repeat_coverage") else "no",
                        "yes" if gate.get("satisfied") else "no",
                    )
                )
                + " |"
            )

    sealed = dict(report.get("sealed_audit") or {})
    sealed_errors = ", ".join(str(item) for item in sealed.get("errors", []))
    lines.extend(
        (
            "",
            "## Sealed Audit",
            "",
            f"- Required: {'yes' if sealed.get('required') else 'no'}",
            f"- Manifest present: {'yes' if sealed.get('manifest_present') else 'no'}",
            f"- Valid: {'yes' if sealed.get('valid') else 'no'}",
            f"- Case IDs: {_md(', '.join(sealed.get('case_ids') or []) or 'none')}",
            f"- Expected holdout SHA-256: {_md(sealed.get('expected_holdout_sha256') or 'none')}",
            f"- Observed holdout SHA-256: {_md(sealed.get('observed_holdout_sha256') or 'none')}",
            f"- Errors: {_md(sealed_errors or 'none')}",
        )
    )
    lines.extend(
        (
            "",
            "## Holdout Topology Summary",
            "",
            *_render_summary_table(dict(splits.get("holdout") or {})),
            "",
            "## Paired Interventions",
            "",
            "| Candidate | Repairs | Regressions | Severe regressions |",
            "| --- | ---: | ---: | ---: |",
        )
    )
    paired = dict(
        dict(splits.get("holdout") or {}).get("paired_interventions") or {}
    )
    if paired:
        for topology, intervention in sorted(paired.items()):
            intervention = dict(intervention)
            counts = dict(intervention.get("counts") or {})
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md(topology),
                        str(counts.get("repair", 0)),
                        str(counts.get("regression", 0)),
                        str(len(intervention.get("severe_regressions") or [])),
                    )
                )
                + " |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 |")
    lines.extend(
        (
            "",
            "## Candidate Decisions",
            "",
            "| Candidate | Status | Dominates baseline | Reasons |",
            "| --- | --- | --- | --- |",
        )
    )
    decisions = dict(report.get("candidate_decisions") or {})
    if decisions:
        for topology, decision in sorted(decisions.items()):
            decision = dict(decision)
            reasons = ", ".join(str(reason) for reason in decision.get("reasons", []))
            lines.append(
                "| "
                + " | ".join(
                    (
                        _md(topology),
                        _md(decision.get("status")),
                        "yes" if decision.get("dominates_baseline") else "no",
                        _md(reasons or "none"),
                    )
                )
                + " |"
            )
    else:
        lines.append("| none | none | no | no candidates |")

    lines.extend(
        (
            "",
            "## Train Summary",
            "",
            *_render_summary_table(dict(splits.get("train") or {})),
            "",
            "## Warnings",
            "",
        )
    )
    warnings = [str(item) for item in report.get("warnings", [])]
    lines.extend(f"- {_md(warning)}" for warning in warnings)
    if not warnings:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _render_summary_table(split_report: Mapping[str, Any]) -> list[str]:
    lines = [
        "| Topology | Cases | Runs | Minimum repeats | Recall | False-negative rate | Completion rate | Terminal anomalies | Unique findings | Unexpected findings | Mean tokens | Token coverage | Mean duration ms | Duration coverage | Mean tool calls | Mean retries | Value feedback |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    topologies = dict(split_report.get("topologies") or {})
    if not topologies:
        lines.append(
            "| none | 0 | 0 | 0 | unavailable | unavailable | unavailable | 0 | 0 | 0 | unavailable | 0/0 | unavailable | 0/0 | unavailable | unavailable | unavailable |"
        )
        return lines
    for topology, summary in sorted(topologies.items()):
        summary = dict(summary)
        feedback = dict(summary.get("value_feedback") or {})
        lines.append(
            "| "
            + " | ".join(
                (
                    _md(topology),
                    str(summary.get("case_count", 0)),
                    str(summary.get("run_count", 0)),
                    str(summary.get("minimum_repeats_observed", 0)),
                    _metric(summary.get("critical_recall")),
                    _metric(summary.get("false_negative_rate")),
                    _metric(summary.get("completion_rate")),
                    str(summary.get("terminal_anomaly_count", 0)),
                    str(summary.get("unique_finding_count", 0)),
                    str(summary.get("unexpected_finding_count", 0)),
                    _metric(summary.get("mean_total_tokens")),
                    _coverage(
                        summary.get("total_tokens_available_count", 0),
                        summary.get("run_count", 0),
                    ),
                    _metric(summary.get("mean_duration_ms")),
                    _coverage(
                        summary.get("duration_ms_available_count", 0),
                        summary.get("run_count", 0),
                    ),
                    _metric(summary.get("mean_tool_calls")),
                    _metric(summary.get("mean_retry_count")),
                    _md(feedback.get("status") or "unavailable"),
                )
            )
            + " |"
        )
    return lines


def _metric(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _coverage(available: Any, total: Any) -> str:
    return f"{int(available or 0)}/{int(total or 0)}"


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _sealed_audit_result(
    dataset: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None,
    required: bool,
) -> dict[str, Any]:
    if manifest is not None:
        result = validate_sealed_audit_manifest(dataset, manifest)
        return {
            **result,
            "required": required,
            "manifest_present": True,
        }
    observed = build_sealed_audit_manifest(dataset)
    return {
        "valid": not required,
        "errors": ["sealed_manifest_required"] if required else [],
        "case_ids": observed["case_ids"],
        "expected_holdout_sha256": "",
        "observed_holdout_sha256": observed["holdout_sha256"],
        "required": required,
        "manifest_present": False,
    }


def _sealed_holdout_cases(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        if case["split"] != "holdout":
            continue
        runs = [
            {
                **run,
                "found_finding_ids": sorted(run["found_finding_ids"]),
                "cost_value_summary": dict(run["cost_value_summary"]),
            }
            for run in sorted(
                case["runs"],
                key=lambda item: (item["topology"], item["repeat_index"]),
            )
        ]
        cases.append(
            {
                "case_id": case["case_id"],
                "split": "holdout",
                "difficulty": case["difficulty"],
                "severity": case["severity"],
                "expected_finding_ids": sorted(case["expected_finding_ids"]),
                "runs": runs,
            }
        )
    return sorted(cases, key=lambda item: item["case_id"])


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _normalize_dataset(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise OrchestrationEvaluationError("evaluation dataset must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise OrchestrationEvaluationError(
            f"schema_version must be {SCHEMA_VERSION}"
        )
    dataset_id = _required_string(payload.get("dataset_id"), "dataset_id")
    baseline_topology = _required_string(
        payload.get("baseline_topology"), "baseline_topology"
    )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise OrchestrationEvaluationError("cases must be a list")

    cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    topologies: set[str] = set()
    repeat_keys: set[tuple[str, str, int]] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise OrchestrationEvaluationError("each case must be an object")
        case_id = _required_string(raw_case.get("case_id"), "case_id")
        if case_id in case_ids:
            raise OrchestrationEvaluationError(f"duplicate case_id: {case_id}")
        case_ids.add(case_id)
        split = _required_string(raw_case.get("split"), f"{case_id}.split")
        if split not in VALID_SPLITS:
            raise OrchestrationEvaluationError(
                f"{case_id}.split must be one of {sorted(VALID_SPLITS)}"
            )
        difficulty = _required_string(
            raw_case.get("difficulty"), f"{case_id}.difficulty"
        )
        severity = _required_string(
            raw_case.get("severity", "medium"),
            f"{case_id}.severity",
        )
        if severity not in VALID_SEVERITIES:
            raise OrchestrationEvaluationError(
                f"{case_id}.severity must be one of {sorted(VALID_SEVERITIES)}"
            )
        expected = _string_ids(
            raw_case.get("expected_finding_ids"),
            f"{case_id}.expected_finding_ids",
        )
        raw_runs = raw_case.get("runs")
        if not isinstance(raw_runs, list) or not raw_runs:
            raise OrchestrationEvaluationError(f"{case_id}.runs must be a non-empty list")
        runs: list[dict[str, Any]] = []
        for raw_run in raw_runs:
            run = _normalize_run(raw_run, case_id=case_id)
            key = (case_id, run["topology"], run["repeat_index"])
            if key in repeat_keys:
                raise OrchestrationEvaluationError(
                    "duplicate repeat_index for "
                    f"case_id={case_id}, topology={run['topology']}: "
                    f"{run['repeat_index']}"
                )
            repeat_keys.add(key)
            topologies.add(run["topology"])
            runs.append(run)
        cases.append(
            {
                "case_id": case_id,
                "split": split,
                "difficulty": difficulty,
                "severity": severity,
                "expected_finding_ids": expected,
                "runs": runs,
            }
        )

    if baseline_topology not in topologies:
        raise OrchestrationEvaluationError(
            f"baseline_topology is absent from runs: {baseline_topology}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "baseline_topology": baseline_topology,
        "cases": cases,
    }


def _normalize_run(raw_run: Any, *, case_id: str) -> dict[str, Any]:
    if not isinstance(raw_run, Mapping):
        raise OrchestrationEvaluationError(f"{case_id}.runs entries must be objects")
    topology = _required_string(raw_run.get("topology"), f"{case_id}.topology")
    repeat_index = raw_run.get("repeat_index")
    if (
        isinstance(repeat_index, bool)
        or not isinstance(repeat_index, int)
        or repeat_index < 1
    ):
        raise OrchestrationEvaluationError(
            f"{case_id}.{topology}.repeat_index must be a positive integer"
        )
    status = _required_string(
        raw_run.get("status"), f"{case_id}.{topology}.status"
    )
    if status not in VALID_STATUSES:
        raise OrchestrationEvaluationError(
            f"{case_id}.{topology}.status must be one of {sorted(VALID_STATUSES)}"
        )
    cost_value_summary = _required_field(
        raw_run,
        "cost_value_summary",
        f"{case_id}.{topology}.cost_value_summary",
    )
    if not isinstance(cost_value_summary, Mapping):
        raise OrchestrationEvaluationError(
            f"{case_id}.{topology}.cost_value_summary must be an object"
        )
    normalized_cost_value_summary = dict(cost_value_summary)
    unknown_cost_keys = sorted(
        set(normalized_cost_value_summary) - _ALLOWED_COST_VALUE_KEYS
    )
    if unknown_cost_keys:
        raise OrchestrationEvaluationError(
            f"{case_id}.{topology} has unknown cost_value_summary keys: "
            + ", ".join(unknown_cost_keys)
        )
    for key in COST_VALUE_SUMMARY_KEYS:
        if key in normalized_cost_value_summary:
            normalized_cost_value_summary[key] = _nonnegative_int(
                normalized_cost_value_summary[key],
                f"{case_id}.{topology}.cost_value_summary.{key}",
            )
    return {
        "topology": topology,
        "repeat_index": repeat_index,
        "status": status,
        "found_finding_ids": _string_ids(
            raw_run.get("found_finding_ids"),
            f"{case_id}.{topology}.found_finding_ids",
        ),
        "total_tokens": _optional_nonnegative_int(
            _required_field(
                raw_run,
                "total_tokens",
                f"{case_id}.{topology}.total_tokens",
            ),
            f"{case_id}.{topology}.total_tokens",
        ),
        "duration_ms": _optional_nonnegative_int(
            _required_field(
                raw_run,
                "duration_ms",
                f"{case_id}.{topology}.duration_ms",
            ),
            f"{case_id}.{topology}.duration_ms",
        ),
        "tool_calls": _nonnegative_int(
            _required_field(
                raw_run,
                "tool_calls",
                f"{case_id}.{topology}.tool_calls",
            ),
            f"{case_id}.{topology}.tool_calls",
        ),
        "retry_count": _nonnegative_int(
            _required_field(
                raw_run,
                "retry_count",
                f"{case_id}.{topology}.retry_count",
            ),
            f"{case_id}.{topology}.retry_count",
        ),
        "terminal_anomaly_count": _nonnegative_int(
            _required_field(
                raw_run,
                "terminal_anomaly_count",
                f"{case_id}.{topology}.terminal_anomaly_count",
            ),
            f"{case_id}.{topology}.terminal_anomaly_count",
        ),
        "cost_value_summary": normalized_cost_value_summary,
    }


def _build_split_report(
    cases: list[dict[str, Any]],
    *,
    baseline_topology: str,
    minimum_cases: int,
    minimum_repeats: int,
) -> dict[str, Any]:
    topologies = sorted(
        {run["topology"] for case in cases for run in case["runs"]}
    )
    unique_counts = _unique_finding_counts(cases)
    case_ids_by_topology = {
        topology: {
            case["case_id"]
            for case in cases
            if any(run["topology"] == topology for run in case["runs"])
        }
        for topology in topologies
    }
    baseline_case_ids = case_ids_by_topology.get(baseline_topology, set())
    repeat_keys_by_topology = {
        topology: {
            (case["case_id"], run["repeat_index"])
            for case in cases
            for run in case["runs"]
            if run["topology"] == topology
        }
        for topology in topologies
    }
    baseline_repeat_keys = repeat_keys_by_topology.get(baseline_topology, set())
    topology_summaries: dict[str, dict[str, Any]] = {}
    evidence_gates: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for topology in topologies:
        summary = _topology_summary(
            cases,
            topology=topology,
            unique_finding_count=unique_counts.get(topology, 0),
        )
        topology_case_ids = case_ids_by_topology[topology]
        paired_case_coverage = (
            topology == baseline_topology or topology_case_ids == baseline_case_ids
        )
        topology_repeat_keys = repeat_keys_by_topology[topology]
        paired_repeat_coverage = (
            topology == baseline_topology
            or topology_repeat_keys == baseline_repeat_keys
        )
        gate = {
            "case_count": summary["case_count"],
            "minimum_cases_required": minimum_cases,
            "minimum_repeats_observed": summary["minimum_repeats_observed"],
            "minimum_repeats_required": minimum_repeats,
            "paired_case_coverage": paired_case_coverage,
            "paired_repeat_coverage": paired_repeat_coverage,
            "missing_baseline_case_ids": sorted(
                baseline_case_ids - topology_case_ids
            ),
            "extra_candidate_case_ids": sorted(
                topology_case_ids - baseline_case_ids
            ),
            "missing_baseline_repeats": _repeat_key_records(
                baseline_repeat_keys - topology_repeat_keys
            ),
            "extra_candidate_repeats": _repeat_key_records(
                topology_repeat_keys - baseline_repeat_keys
            ),
            "satisfied": (
                summary["case_count"] >= minimum_cases
                and summary["minimum_repeats_observed"] >= minimum_repeats
                and paired_case_coverage
                and paired_repeat_coverage
            ),
        }
        summary["evidence_gate_satisfied"] = gate["satisfied"]
        topology_summaries[topology] = summary
        evidence_gates[topology] = gate
        if summary["case_count"] < minimum_cases:
            warnings.append(f"{topology}:case_count_below_minimum")
        if summary["minimum_repeats_observed"] < minimum_repeats:
            warnings.append(f"{topology}:repeat_count_below_minimum")
        if not paired_case_coverage:
            warnings.append(f"{topology}:case_coverage_differs_from_baseline")
        if not paired_repeat_coverage:
            warnings.append(f"{topology}:repeat_coverage_differs_from_baseline")
        if 0 < summary["total_tokens_available_count"] < summary["run_count"]:
            warnings.append(f"{topology}:partial_total_tokens_coverage")
        if 0 < summary["duration_ms_available_count"] < summary["run_count"]:
            warnings.append(f"{topology}:partial_duration_ms_coverage")
    return {
        "case_count": len(cases),
        "minimum_cases_required": minimum_cases,
        "minimum_repeats_required": minimum_repeats,
        "evidence_gates": evidence_gates,
        "paired_interventions": {
            topology: _paired_intervention(
                cases,
                baseline_topology=baseline_topology,
                candidate_topology=topology,
            )
            for topology in topologies
            if topology != baseline_topology
        },
        "topologies": topology_summaries,
        "warnings": warnings,
    }


def _topology_summary(
    cases: list[dict[str, Any]],
    *,
    topology: str,
    unique_finding_count: int,
) -> dict[str, Any]:
    matching: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (case, run)
        for case in cases
        for run in case["runs"]
        if run["topology"] == topology
    ]
    case_counts = Counter(case["case_id"] for case, _run in matching)
    expected_opportunities = 0
    found_expected = 0
    unexpected_finding_count = 0
    value_counts: Counter[str] = Counter({key: 0 for key in _VALUE_KEYS})
    for case, run in matching:
        expected = set(case["expected_finding_ids"])
        found = set(run["found_finding_ids"])
        expected_opportunities += len(expected)
        found_expected += len(expected & found)
        unexpected_finding_count += len(found - expected)
        feedback = build_value_feedback_summary(run["cost_value_summary"])
        value_counts.update(feedback["summary"])
    run_count = len(matching)
    total_token_values = [run["total_tokens"] for _case, run in matching]
    duration_values = [run["duration_ms"] for _case, run in matching]
    total_tokens_available_count = sum(
        value is not None for value in total_token_values
    )
    duration_ms_available_count = sum(
        value is not None for value in duration_values
    )
    critical_recall = (
        found_expected / expected_opportunities if expected_opportunities else 0.0
    )
    return {
        "case_count": len(case_counts),
        "run_count": run_count,
        "minimum_repeats_observed": min(case_counts.values(), default=0),
        "critical_recall": critical_recall,
        "false_negative_rate": (
            (expected_opportunities - found_expected) / expected_opportunities
            if expected_opportunities
            else 0.0
        ),
        "completion_rate": (
            sum(run["status"] == "completed" for _case, run in matching) / run_count
            if run_count
            else 0.0
        ),
        "terminal_anomaly_count": sum(
            run["terminal_anomaly_count"] for _case, run in matching
        ),
        "unique_finding_count": unique_finding_count,
        "unexpected_finding_count": unexpected_finding_count,
        "mean_total_tokens": _mean_optional(total_token_values),
        "total_tokens_available_count": total_tokens_available_count,
        "total_tokens_missing_count": run_count - total_tokens_available_count,
        "mean_duration_ms": _mean_optional(duration_values),
        "duration_ms_available_count": duration_ms_available_count,
        "duration_ms_missing_count": run_count - duration_ms_available_count,
        "mean_tool_calls": _mean_optional(
            run["tool_calls"] for _case, run in matching
        ),
        "mean_retry_count": _mean_optional(
            run["retry_count"] for _case, run in matching
        ),
        "value_feedback": build_value_feedback_summary(dict(value_counts)),
    }


def _unique_finding_counts(cases: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for case in cases:
        expected = set(case["expected_finding_ids"])
        finders: dict[tuple[int, str], set[str]] = {}
        for run in case["runs"]:
            for finding_id in expected & set(run["found_finding_ids"]):
                finders.setdefault((run["repeat_index"], finding_id), set()).add(
                    run["topology"]
                )
        for topologies in finders.values():
            if len(topologies) == 1:
                counts.update(topologies)
    return counts


def _paired_intervention(
    cases: list[dict[str, Any]],
    *,
    baseline_topology: str,
    candidate_topology: str,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    counts = Counter(
        {
            "repair": 0,
            "regression": 0,
            "unchanged_success": 0,
            "unchanged_failure": 0,
        }
    )
    for case in sorted(cases, key=lambda item: item["case_id"]):
        baseline_passed = _case_topology_passed(case, baseline_topology)
        candidate_passed = _case_topology_passed(case, candidate_topology)
        if baseline_passed is None or candidate_passed is None:
            continue
        if not baseline_passed and candidate_passed:
            classification = "repair"
        elif baseline_passed and not candidate_passed:
            classification = "regression"
        elif baseline_passed:
            classification = "unchanged_success"
        else:
            classification = "unchanged_failure"
        counts[classification] += 1
        records.append(
            {
                "case_id": case["case_id"],
                "severity": case["severity"],
                "baseline_passed": baseline_passed,
                "candidate_passed": candidate_passed,
                "classification": classification,
            }
        )
    return {
        "cases": records,
        "counts": dict(counts),
        "repairs": [
            record["case_id"]
            for record in records
            if record["classification"] == "repair"
        ],
        "regressions": [
            record["case_id"]
            for record in records
            if record["classification"] == "regression"
        ],
        "severe_regressions": [
            record["case_id"]
            for record in records
            if record["classification"] == "regression"
            and record["severity"] in {"high", "critical"}
        ],
    }


def _case_topology_passed(
    case: Mapping[str, Any],
    topology: str,
) -> bool | None:
    runs = [run for run in case["runs"] if run["topology"] == topology]
    if not runs:
        return None
    expected = set(case["expected_finding_ids"])
    return all(
        run["status"] == "completed"
        and run["terminal_anomaly_count"] == 0
        and expected <= set(run["found_finding_ids"])
        for run in runs
    )


def _candidate_decisions(
    holdout_report: Mapping[str, Any],
    *,
    baseline_topology: str,
    candidate_topologies: list[str],
) -> dict[str, Any]:
    topologies = dict(holdout_report.get("topologies") or {})
    gates = dict(holdout_report.get("evidence_gates") or {})
    paired_interventions = dict(
        holdout_report.get("paired_interventions") or {}
    )
    baseline = topologies.get(baseline_topology)
    baseline_gate = dict(gates.get(baseline_topology) or {})
    decisions: dict[str, Any] = {}
    for topology in candidate_topologies:
        candidate = topologies.get(topology)
        candidate_gate = dict(gates.get(topology) or {})
        reasons: list[str] = []
        evidence_satisfied = bool(
            baseline
            and candidate
            and baseline_gate.get("satisfied")
            and candidate_gate.get("satisfied")
        )
        if not evidence_satisfied:
            reasons.append("insufficient_evidence")
            if not baseline:
                reasons.append("missing_baseline_holdout_evidence")
            if not candidate:
                reasons.append("missing_candidate_holdout_evidence")
            decisions[topology] = {
                "status": "insufficient_evidence",
                "dominates_baseline": False,
                "reasons": reasons,
            }
            continue

        non_regression = _quality_non_regression(
            candidate, baseline, reasons=reasons
        )
        paired_regression = bool(
            dict(paired_interventions.get(topology) or {}).get("regressions")
        )
        if paired_regression:
            reasons.append("paired_regression_detected")
        improvements = _strict_improvements(candidate, baseline)
        if non_regression:
            reasons.append("quality_non_regression")
        reasons.extend(improvements)
        if not improvements:
            reasons.append("no_strict_improvement")
        dominates = (
            evidence_satisfied
            and non_regression
            and not paired_regression
            and bool(improvements)
        )
        decisions[topology] = {
            "status": (
                "eligible_for_operator_review"
                if dominates
                else "does_not_dominate"
            ),
            "dominates_baseline": dominates,
            "reasons": reasons,
        }
    return decisions


def _quality_non_regression(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    reasons: list[str],
) -> bool:
    checks = (
        ("critical_recall", lambda left, right: left >= right, "critical_recall_regression"),
        ("completion_rate", lambda left, right: left >= right, "completion_rate_regression"),
        (
            "false_negative_rate",
            lambda left, right: left <= right,
            "false_negative_regression",
        ),
        (
            "terminal_anomaly_count",
            lambda left, right: left <= right,
            "terminal_anomaly_regression",
        ),
    )
    ok = True
    for metric, predicate, reason in checks:
        if not predicate(candidate[metric], baseline[metric]):
            reasons.append(reason)
            ok = False
    return ok


def _strict_improvements(
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> list[str]:
    improvements: list[str] = []
    comparisons = (
        ("critical_recall", "higher_critical_recall", lambda left, right: left > right),
        ("completion_rate", "higher_completion_rate", lambda left, right: left > right),
        (
            "false_negative_rate",
            "lower_false_negative_rate",
            lambda left, right: left < right,
        ),
        (
            "terminal_anomaly_count",
            "lower_terminal_anomaly_count",
            lambda left, right: left < right,
        ),
        ("mean_tool_calls", "lower_mean_tool_calls", lambda left, right: left < right),
        ("mean_retry_count", "lower_mean_retry_count", lambda left, right: left < right),
    )
    for metric, reason, predicate in comparisons:
        candidate_value = candidate[metric]
        baseline_value = baseline[metric]
        if (
            candidate_value is not None
            and baseline_value is not None
            and predicate(candidate_value, baseline_value)
        ):
            improvements.append(reason)
    for metric, coverage_key, reason in (
        (
            "mean_total_tokens",
            "total_tokens_available_count",
            "lower_mean_total_tokens",
        ),
        (
            "mean_duration_ms",
            "duration_ms_available_count",
            "lower_mean_duration_ms",
        ),
    ):
        candidate_value = candidate[metric]
        baseline_value = baseline[metric]
        if (
            candidate[coverage_key] == candidate["run_count"]
            and baseline[coverage_key] == baseline["run_count"]
            and candidate_value is not None
            and baseline_value is not None
            and candidate_value < baseline_value
        ):
            improvements.append(reason)
    return improvements


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationEvaluationError(f"{field} must be a non-empty string")
    return value.strip()


def _repeat_key_records(keys: set[tuple[str, int]]) -> list[dict[str, Any]]:
    return [
        {"case_id": case_id, "repeat_index": repeat_index}
        for case_id, repeat_index in sorted(keys)
    ]


def _required_field(mapping: Mapping[str, Any], key: str, field: str) -> Any:
    if key not in mapping:
        raise OrchestrationEvaluationError(f"{field} is required")
    return mapping[key]


def _string_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise OrchestrationEvaluationError(f"{field} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _required_string(item, field)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrchestrationEvaluationError(f"{field} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field)


def _mean_optional(values: Any) -> float | None:
    available = [value for value in values if value is not None]
    return fmean(available) if available else None
