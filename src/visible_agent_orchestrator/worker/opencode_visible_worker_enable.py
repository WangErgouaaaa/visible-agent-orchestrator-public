"""Re-enable recovered OpenCode visible workers into the auto-submit pool.

This module is the symmetric counterpart to stale-worker disable. It only
updates the project-local visible worker registry after explicit authorization.
It never starts/stops workers, sends tmux input, or changes the default worker.
The only policy change allowed is promoting a non-default recovered worker from
allow_auto_submit=false to true after strong current live evidence.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.opencode_visible_registry import (
    VisibleRegistryConflict,
    mutate_visible_registry,
)


SCHEMA_VERSION = 1
UPDATE_KIND = "opencode-visible-worker-enable"
ALLOWED_POLICY_FIELDS = {
    "allow_auto_submit",
    "status",
    "warnings",
    "notes",
}
REBIND_EVIDENCE_MAX_AGE_SECONDS = 300
REBIND_EVIDENCE_FUTURE_SKEW_SECONDS = 30
REGISTRY_IDENTITY_FIELDS = (
    "instance_id",
    "identity_strength",
    "pane_id",
    "pane_pid",
    "pane_start_time_ticks",
    "opencode_pid",
    "opencode_start_time_ticks",
    "port",
    "tmux_session",
    "tmux_window",
    "host_boot_id",
    "tmux_server_pid",
    "binding_generation",
    "fencing_generation",
)
LIVE_IDENTITY_FIELDS = (
    "pane_id",
    "pane_pid",
    "pane_start_time_ticks",
    "opencode_pid",
    "opencode_start_time_ticks",
    "port",
    "tmux_session",
    "tmux_window",
    "host_boot_id",
    "tmux_server_pid",
)
INTEGER_IDENTITY_FIELDS = {
    "pane_pid",
    "pane_start_time_ticks",
    "opencode_pid",
    "opencode_start_time_ticks",
    "port",
    "tmux_server_pid",
    "binding_generation",
    "fencing_generation",
}


@dataclass(frozen=True)
class OpenCodeVisibleWorkerEnableRequest:
    root: str
    worker_id: str
    opencode_visible_registry_path: str
    allow_framework_registry: bool = False
    allow_worker_enable: bool = False
    proposed_status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_visible_worker_enable_plan(
    request: OpenCodeVisibleWorkerEnableRequest,
    *,
    rebind_check: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(request.root).resolve()
    registry_path = Path(request.opencode_visible_registry_path).resolve()
    registry_payload = _read_json(registry_path) if registry_path.exists() else {}
    current = _find_worker(registry_payload, request.worker_id)
    blocked: list[str] = []

    if not _is_framework_opencode_visible_registry(root, registry_path):
        blocked.append(f"registry path outside allowed OpenCode visible framework registry: {registry_path}")
    if not registry_path.exists():
        blocked.append(f"registry missing: {registry_path}")
    if not current:
        blocked.append(f"worker missing: {request.worker_id}")

    if current:
        if bool(current.get("default_for_new_tasks")):
            blocked.append("default worker enable path is not supported by this command")
        if bool(current.get("allow_auto_submit", False)):
            blocked.append("worker already has allow_auto_submit=true")

    evidence = rebind_check or {}
    if not evidence:
        blocked.append("missing rebind-check evidence")
    else:
        blocked.extend(
            _validate_rebind_evidence_identity(
                root=root,
                worker_id=request.worker_id,
                current=current,
                evidence=evidence,
                now=now,
            )
        )
        if not bool(evidence.get("ok")):
            blocked.append("worker does not currently pass rebind-check")
        status = str(evidence.get("status") or "")
        if status != "current_binding_ok":
            blocked.append(f"rebind-check status must be current_binding_ok, got: {status or '-'}")
        if str(evidence.get("binding_confidence") or "") != "strong":
            blocked.append("rebind-check must provide strong binding_confidence")
        binding = evidence.get("binding", {}) if isinstance(evidence.get("binding"), dict) else {}
        live = evidence.get("live", {}) if isinstance(evidence.get("live"), dict) else {}
        checks = binding.get("checks", {}) if isinstance(binding.get("checks"), dict) else {}
        if not checks:
            checks = live.get("checks", {}) if isinstance(live.get("checks"), dict) else {}
        if not bool(checks.get("required_tui_present")):
            blocked.append("rebind-check must prove required /tui endpoints are present")
        if not bool(checks.get("runtime_adapter_verify_ok")):
            blocked.append("rebind-check must prove runtime_adapter_verify_ok")

    proposed = dict(current or {})
    if current:
        proposed["allow_auto_submit"] = True
        proposed["status"] = request.proposed_status
        warnings_list = [str(item) for item in proposed.get("warnings", [])] if isinstance(proposed.get("warnings"), list) else []
        disable_warning = "disabled from auto-submit after live rebind-check binding_failed evidence"
        warnings_list = [item for item in warnings_list if item != disable_warning]
        enable_warning = "re-enabled for auto-submit after live rebind-check current_binding_ok evidence"
        if enable_warning not in warnings_list:
            warnings_list.append(enable_warning)
        proposed["warnings"] = warnings_list
        notes_list = [str(item) for item in proposed.get("notes", [])] if isinstance(proposed.get("notes"), list) else []
        disable_note = "Auto submit disabled because live rebind-check evidence failed."
        notes_list = [item for item in notes_list if item != disable_note]
        enable_note = "Auto submit re-enabled because live rebind-check evidence is current again."
        if enable_note not in notes_list:
            notes_list.append(enable_note)
        proposed["notes"] = notes_list

    changed_fields = _changed_fields(current, proposed) if current else []
    forbidden_changed = sorted(field for field in changed_fields if field not in ALLOWED_POLICY_FIELDS)
    if forbidden_changed:
        blocked.append(f"non-enable fields would change: {forbidden_changed}")

    next_payload = _replace_worker(registry_payload, request.worker_id, proposed) if current else registry_payload
    blocked = _dedupe(blocked)
    return {
        "schema_version": SCHEMA_VERSION,
        "update_kind": UPDATE_KIND,
        "created_at": _utc_now(),
        "ok": not blocked,
        "root": str(root),
        "worker_id": request.worker_id,
        "registry_path": str(registry_path),
        "current_registry_record": current,
        "proposed_registry_record": proposed,
        "changed_fields": changed_fields,
        "forbidden_changed_fields": forbidden_changed,
        "safe_for_enable": not blocked,
        "blocked_reasons": blocked,
        "warnings": [],
        "rebind_check": evidence,
        "rebind_evidence_policy": {
            "max_age_seconds": REBIND_EVIDENCE_MAX_AGE_SECONDS,
            "future_skew_seconds": REBIND_EVIDENCE_FUTURE_SKEW_SECONDS,
            "registry_identity_fields": list(
                REGISTRY_IDENTITY_FIELDS
            ),
            "live_identity_fields": list(LIVE_IDENTITY_FIELDS),
        },
        "pre_hash": _sha256_file(registry_path),
        "proposed_hash": _sha256_json(next_payload) if current else "",
        "would_write_registry": False,
        "would_execute_live": False,
        "requires_allow_framework_registry": True,
        "requires_allow_worker_enable": True,
        "no_live_actions_performed": True,
        "safe_next_actions": [
            "run opencode-visible-worker-enable with explicit allow flags",
            "rerun backend-audit after registry write",
        ] if not blocked else [
            "repair the worker and gather current_binding_ok rebind-check evidence",
        ],
    }


def write_opencode_visible_worker_enable(
    request: OpenCodeVisibleWorkerEnableRequest,
    *,
    rebind_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = build_opencode_visible_worker_enable_plan(request, rebind_check=rebind_check)
    blocked = list(plan["blocked_reasons"])
    if not request.allow_framework_registry:
        blocked.append("missing --allow-framework-registry")
    if not request.allow_worker_enable:
        blocked.append("missing --allow-worker-enable")
    blocked = _dedupe(blocked)
    if blocked:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": bool(request.allow_worker_enable),
            "wrote_registry": False,
            "registry_path": plan["registry_path"],
            "worker_id": request.worker_id,
            "changed_fields": plan["changed_fields"],
            "blocked_reasons": blocked,
            "warnings": [],
            "would_execute_live": False,
            "no_live_actions_performed": True,
        }

    registry_path = Path(request.opencode_visible_registry_path).resolve()
    try:
        mutation = mutate_visible_registry(
            registry_path,
            lambda payload: _replace_worker(payload, request.worker_id, plan["proposed_registry_record"]),
            expected_pre_hash=plan["pre_hash"],
            backup_factory=_create_backup,
        )
    except VisibleRegistryConflict as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": True,
            "wrote_registry": False,
            "registry_path": str(registry_path),
            "worker_id": request.worker_id,
            "changed_fields": plan["changed_fields"],
            "blocked_reasons": [str(exc)],
            "warnings": [],
            "would_execute_live": False,
            "no_live_actions_performed": True,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "attempted": True,
        "wrote_registry": True,
        "atomic_write": True,
        "backup_path": mutation["backup_path"],
        "registry_path": str(registry_path),
        "worker_id": request.worker_id,
        "changed_fields": plan["changed_fields"],
        "pre_hash": mutation["pre_hash"],
        "post_hash": mutation["post_hash"],
        "blocked_reasons": [],
        "warnings": [],
        "would_execute_live": False,
        "no_live_actions_performed": True,
    }


def validate_opencode_visible_worker_enable_record(payload: dict[str, Any]) -> dict[str, Any]:
    blocked: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        blocked.append("schema_version must be 1")
    if payload.get("update_kind") != UPDATE_KIND:
        blocked.append(f"update_kind must be {UPDATE_KIND}")
    if payload.get("would_execute_live") is not False:
        blocked.append("would_execute_live must be false")
    if payload.get("would_write_registry") is not False:
        blocked.append("plan would_write_registry must be false")
    if payload.get("safe_for_enable") is not True:
        blocked.append("safe_for_enable must be true")
    for field_name in _list(payload.get("changed_fields")):
        if str(field_name) not in ALLOWED_POLICY_FIELDS:
            blocked.append(f"changed field is not an enable-only policy field: {field_name}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not blocked,
        "blocked_reasons": _dedupe(blocked),
    }


def _is_framework_opencode_visible_registry(root: Path, registry_path: Path) -> bool:
    allowed = (root / ".omx" / "framework" / "team" / "opencode_visible_workers.json").resolve()
    return registry_path.resolve() == allowed


def _validate_rebind_evidence_identity(
    *,
    root: Path,
    worker_id: str,
    current: dict[str, Any],
    evidence: dict[str, Any],
    now: datetime | None,
) -> list[str]:
    blocked: list[str] = []
    evidence_worker_id = str(evidence.get("worker_id") or "")
    if evidence_worker_id != worker_id:
        blocked.append(
            "rebind-check worker_id must match requested worker: "
            f"expected {worker_id}, got {evidence_worker_id or '-'}"
        )
    evidence_root = str(evidence.get("root") or "")
    if not evidence_root or Path(evidence_root).resolve() != root:
        blocked.append(
            "rebind-check root must match requested root: "
            f"expected {root}, got {evidence_root or '-'}"
        )

    generated_at = _parse_utc_timestamp(evidence.get("generated_at"))
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if generated_at is None:
        blocked.append(
            "rebind-check generated_at must be a parseable UTC timestamp"
        )
    else:
        age_seconds = (current_time - generated_at).total_seconds()
        if age_seconds > REBIND_EVIDENCE_MAX_AGE_SECONDS:
            blocked.append(
                "rebind-check generated_at is stale: "
                f"age_seconds={int(age_seconds)} "
                f"max={REBIND_EVIDENCE_MAX_AGE_SECONDS}"
            )
        elif age_seconds < -REBIND_EVIDENCE_FUTURE_SKEW_SECONDS:
            blocked.append(
                "rebind-check generated_at is too far in the future: "
                f"skew_seconds={int(-age_seconds)} "
                f"max={REBIND_EVIDENCE_FUTURE_SKEW_SECONDS}"
            )

    registry_identity = (
        evidence.get("registry")
        if isinstance(evidence.get("registry"), dict)
        else {}
    )
    live_identity = (
        evidence.get("live")
        if isinstance(evidence.get("live"), dict)
        else {}
    )
    blocked.extend(
        _identity_mismatches(
            current=current,
            observed=registry_identity,
            scope="registry",
            fields=REGISTRY_IDENTITY_FIELDS,
        )
    )
    blocked.extend(
        _identity_mismatches(
            current=current,
            observed=live_identity,
            scope="live",
            fields=LIVE_IDENTITY_FIELDS,
        )
    )
    return blocked


def _parse_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _identity_mismatches(
    *,
    current: dict[str, Any],
    observed: dict[str, Any],
    scope: str,
    fields: tuple[str, ...],
) -> list[str]:
    blocked: list[str] = []
    for field in fields:
        expected = _identity_value(field, current.get(field))
        if expected in {"", 0}:
            continue
        actual = _identity_value(field, observed.get(field))
        if actual != expected:
            blocked.append(
                f"rebind-check {scope}.{field} must match current registry: "
                f"expected {expected!r}, got {actual!r}"
            )
    return blocked


def _identity_value(field: str, value: Any) -> str | int:
    if field in INTEGER_IDENTITY_FIELDS:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return str(value or "")


def _find_worker(registry: dict[str, Any], worker_id: str) -> dict[str, Any]:
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            return dict(item)
    return {}


def _replace_worker(registry: dict[str, Any], worker_id: str, replacement: dict[str, Any]) -> dict[str, Any]:
    payload = dict(registry)
    workers = []
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            workers.append(dict(replacement))
        else:
            workers.append(item)
    payload["workers"] = workers
    return payload


def _changed_fields(current: dict[str, Any], proposed: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for key in sorted(set(current) | set(proposed)):
        if current.get(key) != proposed.get(key):
            changed.append(key)
    return changed


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256((json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")).hexdigest()


def _create_backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + f".bak.{stamp}")
    shutil.copy2(path, backup)
    return backup
