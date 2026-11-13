from __future__ import annotations

from pathlib import Path
from typing import Any

from visible_agent_orchestrator.terminal_protocol_baseline import (
    _read_json_record,
    _run_root,
    creation_record_path,
    legacy_membership,
)
from visible_agent_orchestrator.terminal_protocol_constants import (
    CREATION_RECORD_KIND,
    CURRENT_BINDING_FIELDS,
    IDENTITY_FIELDS,
    MODERN_RECORDS,
    REQUIRED_MODERN_RECORDS,
    TERMINAL_PROTOCOL_CLASSIFICATION_INVALID,
    TERMINAL_PROTOCOL_CLASSIFICATION_LEGACY,
    TERMINAL_PROTOCOL_CLASSIFICATION_MODERN,
)
from visible_agent_orchestrator.terminal_protocol_identity import record_identity


def _record_kind(name: str) -> str:
    if name == "terminal_output_claim.json":
        return "claim"
    if name == "acceptance_fence.json":
        return "fence"
    return name[:-5]


def _has_alias(payload: dict[str, Any], aliases: tuple[str, ...]) -> bool:
    return any(name in payload for name in aliases)


def _require_modern_record_shape(
    source: str,
    payload: dict[str, Any],
    kind: str,
    marker: bool,
    invalid: list[str],
) -> None:
    if not marker:
        invalid.append(f"{source}.protocol_marker")
        return
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
        invalid.append(f"{source}.schema_version")
    if kind == "task":
        required_aliases = (
            ("task_id", "id", "task"),
            ("run_id", "run"),
            ("worker_id", "owner", "worker"),
            ("attempt_id",),
            ("contract_hash",),
        )
    elif kind == "dispatch":
        required_aliases = (
            ("task_id", "task", "id"),
            ("run_id", "run"),
            ("worker_id", "worker", "owner"),
            ("attempt_id",),
            ("contract_hash",),
        )
    elif kind == "fence":
        required_aliases = (
            ("run_id", "run"),
            ("task_id", "id", "task"),
            ("attempt_id",),
            ("contract_hash",),
            ("worker_id", "worker", "owner"),
            ("session_id",),
            ("instance_id",),
            ("binding_generation",),
            ("fencing_generation",),
        )
    elif kind == "claim":
        required_aliases = (
            ("task_id", "id", "task"),
            ("attempt_id",),
            ("contract_hash",),
            ("worker_id", "worker", "owner"),
            ("session_id",),
            ("instance_id",),
            ("binding_generation",),
            ("fencing_generation",),
        )
    elif kind == "creation":
        required_aliases = (
            ("run_id", "run"),
            ("task_id", "id", "task"),
            ("attempt_id",),
            ("contract_hash",),
        )
        if payload.get("record_kind") != CREATION_RECORD_KIND:
            invalid.append(f"{source}.record_kind")
    else:
        invalid.append(f"{source}.kind")
        return
    for aliases in required_aliases:
        if not _has_alias(payload, aliases):
            invalid.append(f"{source}.{aliases[0]}")


def classify_terminal_protocol(task_dir: Path) -> dict[str, Any]:
    """Return one monotonic terminal identity classification for a task."""
    run_id = _run_root(task_dir).name
    task_id = task_dir.name
    invalid: list[str] = []
    modern_sources: list[str] = []
    identity_values: dict[str, list[Any]] = {field: [] for field in IDENTITY_FIELDS}
    present_records: set[str] = set()
    records: dict[str, tuple[dict[str, Any], bool]] = {}

    def add(source: str, payload: dict[str, Any], kind: str) -> bool:
        identity, marker, errors = record_identity(
            source,
            payload,
            kind=kind,
            run_id=run_id,
            task_id=task_id,
        )
        invalid.extend(errors)
        if marker:
            modern_sources.append(source)
        for field, value in identity.items():
            if kind == "creation" and field in CURRENT_BINDING_FIELDS:
                continue
            identity_values[field].append(value)
        nested = payload.get("acceptance_fence")
        if "acceptance_fence" not in payload:
            return marker
        if not isinstance(nested, dict) or not nested:
            invalid.append(f"{source}.acceptance_fence")
            return marker
        nested_marker = any(
            name in nested
            for name in (
                "task_protocol_version",
                "terminal_protocol_version",
                "terminal_acceptance_required",
            )
        )
        if marker and not nested_marker:
            invalid.append(f"{source}.acceptance_fence.protocol_marker")
            return marker
        if not nested_marker:
            return marker
        add(f"{source}.acceptance_fence", nested, "fence")
        _require_modern_record_shape(
            f"{source}.acceptance_fence",
            nested,
            "fence",
            True,
            invalid,
        )
        return marker

    for name in MODERN_RECORDS:
        present, payload, malformed = _read_json_record(task_dir / name)
        if not present:
            continue
        present_records.add(name)
        if malformed or not payload:
            invalid.append(name)
            continue
        kind = _record_kind(name)
        marker = add(name, payload, kind)
        records[name] = (payload, marker)

    creation_present, creation, creation_malformed = _read_json_record(
        creation_record_path(task_dir)
    )
    if creation_present:
        present_records.add("creation")
        if creation_malformed or not creation:
            invalid.append("terminal_protocol_creation")
        else:
            records["creation"] = (creation, True)
            add("terminal_protocol_creation", creation, "creation")

    modern_evidence = bool(modern_sources) or creation_present
    if modern_evidence:
        if modern_sources and not creation_present:
            invalid.append("terminal_protocol_creation")
        for name in REQUIRED_MODERN_RECORDS:
            if name not in present_records:
                invalid.append(name)
                continue
            if name not in records:
                continue
            payload, marker = records[name]
            _require_modern_record_shape(
                name,
                payload,
                _record_kind(name),
                marker,
                invalid,
            )
        for name in ("terminal_output_claim.json", "creation"):
            if name not in present_records:
                continue
            if name not in records:
                continue
            payload, marker = records[name]
            _require_modern_record_shape(
                name,
                payload,
                "creation" if name == "creation" else "claim",
                marker,
                invalid,
            )
    for field, values in identity_values.items():
        if any(value != values[0] for value in values[1:]):
            invalid.append(field)

    legacy, legacy_errors = legacy_membership(task_dir)
    if modern_sources:
        legacy_errors = [
            error
            for error in legacy_errors
            if error != "legacy_baseline_membership"
        ]
    invalid.extend(legacy_errors)
    if modern_sources and legacy:
        invalid.append("legacy_modern_conflict")
    if not modern_evidence and not legacy:
        invalid.append("legacy_identity_missing")
    invalid = sorted(set(invalid))
    if invalid:
        classification = TERMINAL_PROTOCOL_CLASSIFICATION_INVALID
        reason = "invalid_terminal_protocol_identity"
    elif modern_evidence:
        classification = TERMINAL_PROTOCOL_CLASSIFICATION_MODERN
        reason = "modern_terminal_protocol"
    else:
        classification = TERMINAL_PROTOCOL_CLASSIFICATION_LEGACY
        reason = "legacy_terminal_baseline_membership"
    identity = {
        field: values[0]
        for field, values in identity_values.items()
        if values
    }
    identity.setdefault("run_id", run_id)
    identity.setdefault("task_id", task_id)
    return {
        "classification": classification,
        "reason": reason,
        "modern_sources": sorted(set(modern_sources)),
        "mismatched_fields": invalid,
        "identity": identity,
    }
