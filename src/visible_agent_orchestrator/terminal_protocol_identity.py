from __future__ import annotations

from typing import Any

from visible_agent_orchestrator.terminal_protocol_constants import (
    CREATION_IDENTITY_FIELDS,
    CREATION_RECORD_KIND,
    FENCE_RECORD_KIND,
    IDENTITY_FIELDS,
    OPERATOR_RECOVERY_CLAIM_KIND,
    PROTOCOL_ALIASES,
    TASK_PROTOCOL_VERSION,
    TERMINAL_OUTPUT_CLAIM_KIND,
)


def record_identity(
    source: str,
    payload: dict[str, Any],
    *,
    kind: str,
    run_id: str,
    task_id: str,
) -> tuple[dict[str, Any], bool, list[str]]:
    """Parse one terminal record into normalized identity and validation errors."""
    invalid: list[str] = []
    identity: dict[str, Any] = {}
    explicit_modern = any(
        name in payload for name in (*PROTOCOL_ALIASES, "terminal_acceptance_required")
    )
    marker = kind == "creation" or explicit_modern
    protocol_values = [payload[name] for name in PROTOCOL_ALIASES if name in payload]
    if protocol_values:
        marker = True
        if any(
            type(value) is not int or value != TASK_PROTOCOL_VERSION
            for value in protocol_values
        ):
            invalid.extend(PROTOCOL_ALIASES)
        else:
            identity["task_protocol_version"] = protocol_values[0]
    if "terminal_acceptance_required" in payload:
        marker = True
        value = payload["terminal_acceptance_required"]
        if type(value) is not bool or value is not True:
            invalid.append("terminal_acceptance_required")
        else:
            identity["terminal_acceptance_required"] = value

    aliases = {
        "run_id": ("run_id", "run"),
        "task_id": ("task_id", "id", "task"),
        "worker_id": ("worker_id", "worker", "owner"),
        "attempt_id": ("attempt_id",),
        "contract_hash": ("contract_hash",),
        "session_id": ("session_id",),
        "instance_id": ("instance_id",),
        "binding_generation": ("binding_generation",),
        "fencing_generation": ("fencing_generation",),
    }
    for field, names in aliases.items():
        values = [
            (
                name,
                _normalize_identity_alias(field, payload[name], run_id),
            )
            for name in names
            if name in payload
        ]
        if not values:
            continue
        value = values[0][1]
        identity[field] = value
        for alias, alias_value in values[1:]:
            if alias_value != value:
                invalid.extend((field, alias))

    if kind == "claim" and not explicit_modern:
        claim_kind = payload.get("claim_kind")
        if claim_kind not in {TERMINAL_OUTPUT_CLAIM_KIND, OPERATOR_RECOVERY_CLAIM_KIND}:
            invalid.append("claim_kind")
        else:
            legacy_required = set(IDENTITY_FIELDS) - {
                "task_protocol_version",
                "terminal_acceptance_required",
                "run_id",
            }
            if any(field not in identity for field in legacy_required):
                invalid.extend(sorted(legacy_required - set(identity)))

    if identity.get("run_id") and identity["run_id"] != run_id:
        invalid.append("run_id")
    if identity.get("task_id") and identity["task_id"] != task_id:
        invalid.append("task_id")
    for field in (
        "run_id",
        "task_id",
        "attempt_id",
        "contract_hash",
        "worker_id",
        "instance_id",
    ):
        if field in identity and (not isinstance(identity[field], str) or not identity[field]):
            invalid.append(field)
    if "session_id" in identity and not isinstance(identity["session_id"], str):
        invalid.append("session_id")
    for field in ("binding_generation", "fencing_generation"):
        if field in identity and (type(identity[field]) is not int or identity[field] < 0):
            invalid.append(field)

    if kind == "claim" and explicit_modern:
        claim_kind = payload.get("claim_kind")
        if claim_kind not in {TERMINAL_OUTPUT_CLAIM_KIND, OPERATOR_RECOVERY_CLAIM_KIND}:
            invalid.append("claim_kind")
        required = set(IDENTITY_FIELDS) - {"run_id"}
        if any(field not in identity for field in required):
            invalid.extend(sorted(required - set(identity)))
        if not isinstance(identity.get("session_id"), str) or not identity.get("session_id"):
            invalid.append("session_id")
    elif kind == "creation" and marker:
        if payload.get("record_kind") != CREATION_RECORD_KIND:
            invalid.append("record_kind")
        required = set(CREATION_IDENTITY_FIELDS)
        if any(field not in identity for field in required):
            invalid.extend(sorted(required - set(identity)))
    elif kind == "fence" and marker:
        if payload.get("fence_kind") != FENCE_RECORD_KIND:
            invalid.append("fence_kind")
        required = set(IDENTITY_FIELDS) - {"run_id", "session_id"}
        if any(field not in identity for field in required):
            invalid.extend(sorted(required - set(identity)))
    elif kind in {"task", "dispatch"} and marker and "acceptance_fence" not in payload:
        required = set(IDENTITY_FIELDS) - {"run_id", "session_id"}
        if any(field not in identity for field in required):
            invalid.extend(sorted(required - set(identity)))
    return identity, marker, sorted(set(invalid))


def _normalize_identity_alias(field: str, value: Any, run_id: str) -> Any:
    if field == "run_id" and isinstance(value, str):
        prefix = "visible-runs/"
        if value == f"{prefix}{run_id}":
            return run_id
    return value
