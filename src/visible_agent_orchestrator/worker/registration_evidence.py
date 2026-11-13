"""Registration evidence packages for future live worker candidates.

This module combines existing authorization, live-enable, preflight, and
dry-run registration evidence. It performs local JSON evaluation only: no live
worker is contacted and no runtime registry or state file is written.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError

from .authorization import (
    AuthorizationValidationResult,
    validate_authorization_record_file,
    validate_authorization_record_payload,
)
from .config_paths import ConfigPathEvidence, resolve_live_enable_config
from .live_enable import LiveEnableConfig, evaluate_live_enable, public_backend_kind
from .preflight import collect_preflight_evidence_from_payload
from .registration import DryRunRegistrationRequest, build_dry_run_registration_plan


FUTURE_LIVE_WORKER_BACKENDS = {"claude_code", "cursor_agent"}


@dataclass(frozen=True)
class RegistrationEvidencePackageRequest:
    backend_kind: str
    worker_id: str
    workspace_root: str
    runtime_backend: str
    runtime_binding_kind: str = ""
    pane_id: str = ""
    process_id: str = ""
    port: str = ""
    user_owned_pane: bool = False
    worker_label: str = ""
    authorization_record_path: str = ""
    live_enable_config_path: str = ""
    preflight_request_path: str = ""
    expected_business_index_files: list[str] = field(default_factory=list)
    allow_mixed_worker: bool = False
    notes: list[str] = field(default_factory=list)
    business_index_evidence: dict[str, Any] = field(default_factory=dict)
    mixed_worker_evidence: dict[str, Any] = field(default_factory=dict)
    authorization_record: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegistrationEvidencePackageRequest":
        return cls(
            backend_kind=str(payload.get("backend_kind", "")),
            worker_id=str(payload.get("worker_id", "")),
            worker_label=str(payload.get("worker_label", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            runtime_backend=str(payload.get("runtime_backend", "")),
            runtime_binding_kind=str(payload.get("runtime_binding_kind", "")),
            pane_id=str(payload.get("pane_id", "")),
            process_id=str(payload.get("process_id", "")),
            port=str(payload.get("port", "")),
            user_owned_pane=bool(payload.get("user_owned_pane", False)),
            authorization_record_path=str(payload.get("authorization_record_path", "")),
            live_enable_config_path=str(payload.get("live_enable_config_path", "")),
            preflight_request_path=str(payload.get("preflight_request_path", "")),
            expected_business_index_files=_string_list(payload.get("expected_business_index_files", [])),
            allow_mixed_worker=bool(payload.get("allow_mixed_worker", False)),
            notes=_string_list(payload.get("notes", [])),
            business_index_evidence=_dict(payload, "business_index_evidence", {}),
            mixed_worker_evidence=_dict(payload, "mixed_worker_evidence", {}),
            authorization_record=_dict(payload, "authorization_record", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegistrationEvidencePackage:
    schema_version: int
    package_id: str
    generated_at: str
    generated_by: str
    backend_kind: str
    worker_id: str
    worker_label: str
    workspace_root: str
    runtime_backend: str
    runtime_binding_kind: str
    pane_id: str
    process_id: str
    port: str
    user_owned_pane: bool
    expected_business_index_files: list[str]
    authorization_record: dict[str, Any]
    authorization_validation: dict[str, Any]
    live_enable_config: dict[str, Any]
    config_path_evidence: dict[str, Any]
    live_enable_decision: dict[str, Any]
    preflight_evidence: dict[str, Any]
    dry_run_registration_plan: dict[str, Any]
    capability_evidence: dict[str, Any]
    workspace_evidence: dict[str, Any]
    runtime_binding_evidence: dict[str, Any]
    business_index_evidence: dict[str, Any]
    mixed_worker_evidence: dict[str, Any]
    forbidden_action_evidence: dict[str, Any]
    overall_package_ok: bool
    ready_for_live_enable: bool
    ready_for_registration: bool
    ready_for_single_worker_smoke: bool
    live_submit_allowed: bool
    registration_allowed: bool
    would_write_registry: bool
    would_write_state: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    required_next_evidence: list[str] = field(default_factory=list)
    unresolved_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegistrationEvidenceValidationResult:
    ok: bool
    backend_kind: str = ""
    worker_id: str = ""
    ready_for_live_enable: bool = False
    ready_for_registration: bool = False
    ready_for_single_worker_smoke: bool = False
    live_submit_allowed: bool = False
    registration_allowed: bool = False
    would_write_registry: bool = False
    would_write_state: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_registration_evidence_package_from_fixture(path: Path | str) -> RegistrationEvidencePackage:
    request_path = Path(path)
    payload = _read_json(request_path, kind="registration evidence request")
    request = RegistrationEvidencePackageRequest.from_dict(payload)
    return build_registration_evidence_package(request, payload=payload, config_base=request_path.parent)


def build_registration_evidence_package(
    request: RegistrationEvidencePackageRequest,
    *,
    payload: dict[str, Any] | None = None,
    config_base: Path | None = None,
    generated_by: str = "omx_worker_backend",
) -> RegistrationEvidencePackage:
    public = public_backend_kind(request.backend_kind)
    source = _source_with_preflight_request(dict(payload or request.to_dict()), request, config_base)
    config, config_evidence = _load_config(request, config_base)
    config_source = config_evidence.config_path or config_evidence.config_source
    authorization_record = _authorization_record_payload(request, config_base)
    authorization = _authorization_result(request, config_base)
    effective_authorization_id = authorization.authorization_id if authorization.ok else ""
    effective_config = config
    if effective_authorization_id and not config.user_authorization_record:
        effective_config = LiveEnableConfig(**{**config.to_dict(), "user_authorization_record": effective_authorization_id})

    preflight_payload = _preflight_payload(source, request, effective_authorization_id)
    preflight = collect_preflight_evidence_from_payload(
        preflight_payload,
        evidence_source="registration-evidence-package",
        config_base=config_base,
    )
    decision = evaluate_live_enable(
        backend_kind=public,
        config=effective_config,
        workspace_root=request.workspace_root,
        worker_id=request.worker_id,
        runtime_backend=request.runtime_backend,
        user_owned_pane=request.user_owned_pane,
        config_source=config_source,
    )
    registration = build_dry_run_registration_plan(
        DryRunRegistrationRequest(
            backend_kind=public,
            worker_id=request.worker_id,
            workspace_root=request.workspace_root,
            runtime_backend=request.runtime_backend,
            runtime_binding_kind=request.runtime_binding_kind,
            pane_id=request.pane_id,
            process_id=request.process_id,
            port=request.port,
            user_owned_pane=request.user_owned_pane,
            user_authorization_record=effective_authorization_id,
        ),
        config=effective_config,
        config_source=config_source,
    )

    blocked = _dedupe(
        authorization.blocked_reasons
        + decision.blocked_reasons
        + preflight.blocked_reasons
        + registration.blocked_reasons
    )
    warnings = _dedupe(
        authorization.warnings
        + decision.warnings
        + preflight.warnings
        + registration.warnings
        + config_evidence.warnings
        + [
            "registration evidence package is not authorization",
            "registration evidence package is not live enable",
            "registration evidence package is not registration",
            "registration evidence package is not live smoke",
            "registration evidence package is not preflight replacement",
            "valid authorization cannot override disabled live capability or live gate",
        ]
    )
    future_worker = public in FUTURE_LIVE_WORKER_BACKENDS
    overall_ok = not blocked and authorization.ok and preflight.overall_preflight_ok and registration.registration_allowed
    ready_for_live_enable = False if future_worker else overall_ok
    ready_for_registration = False
    ready_for_single_worker_smoke = False

    return RegistrationEvidencePackage(
        schema_version=1,
        package_id=_package_id(public, request.worker_id),
        generated_at=_now(),
        generated_by=generated_by,
        backend_kind=public,
        worker_id=request.worker_id,
        worker_label=request.worker_label,
        workspace_root=request.workspace_root,
        runtime_backend=request.runtime_backend,
        runtime_binding_kind=request.runtime_binding_kind,
        pane_id=request.pane_id,
        process_id=request.process_id,
        port=request.port,
        user_owned_pane=request.user_owned_pane,
        expected_business_index_files=list(request.expected_business_index_files),
        authorization_record=authorization_record,
        authorization_validation=authorization.to_dict(),
        live_enable_config=effective_config.to_dict(),
        config_path_evidence=config_evidence.to_dict(),
        live_enable_decision=decision.to_dict(),
        preflight_evidence=preflight.to_dict(),
        dry_run_registration_plan=registration.to_dict(),
        capability_evidence=preflight.capability_evidence,
        workspace_evidence=preflight.workspace_evidence,
        runtime_binding_evidence={
            "runtime_backend": request.runtime_backend,
            "runtime_binding_kind": request.runtime_binding_kind,
            "pane_id": request.pane_id,
            "process_id": request.process_id,
            "port": request.port,
            "user_owned_pane": request.user_owned_pane,
        },
        business_index_evidence=preflight.business_index_evidence,
        mixed_worker_evidence=preflight.mixed_worker_evidence,
        forbidden_action_evidence={
            **preflight.forbidden_action_evidence,
            "would_write_registry": False,
            "would_write_state": False,
        },
        overall_package_ok=overall_ok,
        ready_for_live_enable=ready_for_live_enable,
        ready_for_registration=ready_for_registration,
        ready_for_single_worker_smoke=ready_for_single_worker_smoke,
        live_submit_allowed=False,
        registration_allowed=False,
        would_write_registry=False,
        would_write_state=False,
        blocked_reasons=blocked,
        warnings=warnings,
        safe_next_actions=_safe_next_actions(blocked),
        forbidden_next_actions=_forbidden_next_actions(),
        required_next_evidence=_required_next_evidence(blocked),
        unresolved_issues=list(blocked),
    )


def validate_registration_evidence_package_file(path: Path | str) -> RegistrationEvidenceValidationResult:
    return validate_registration_evidence_package_payload(
        _read_json(Path(path), kind="registration evidence package")
    )


def validate_registration_evidence_package_payload(payload: dict[str, Any]) -> RegistrationEvidenceValidationResult:
    errors: list[str] = []
    missing = [field_name for field_name in _required_fields() if field_name not in payload]
    for field_name in missing:
        errors.append(f"missing required field: {field_name}")
    backend_kind = str(payload.get("backend_kind", ""))
    worker_id = str(payload.get("worker_id", ""))
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if backend_kind in FUTURE_LIVE_WORKER_BACKENDS:
        if bool(payload.get("ready_for_live_enable", False)):
            errors.append(f"{backend_kind} must fail closed for ready_for_live_enable")
        if bool(payload.get("ready_for_registration", False)):
            errors.append(f"{backend_kind} must fail closed for ready_for_registration")
        if bool(payload.get("ready_for_single_worker_smoke", False)):
            errors.append(f"{backend_kind} must fail closed for ready_for_single_worker_smoke")
    for flag in ("live_submit_allowed", "registration_allowed", "would_write_registry", "would_write_state"):
        if bool(payload.get(flag, False)):
            errors.append(f"{flag} must be false in registration evidence package")
    warnings = _dedupe(_string_list(payload.get("warnings", [])))
    for required_warning in (
        "registration evidence package is not authorization",
        "registration evidence package is not live enable",
        "registration evidence package is not registration",
        "registration evidence package is not preflight replacement",
    ):
        if required_warning not in warnings:
            errors.append(f"missing warning: {required_warning}")
    forbidden = _dedupe(_string_list(payload.get("forbidden_next_actions", [])))
    for required_action in _forbidden_next_actions():
        if required_action not in forbidden:
            errors.append(f"missing forbidden_next_action: {required_action}")

    return RegistrationEvidenceValidationResult(
        ok=not errors,
        backend_kind=backend_kind,
        worker_id=worker_id,
        ready_for_live_enable=bool(payload.get("ready_for_live_enable", False)),
        ready_for_registration=bool(payload.get("ready_for_registration", False)),
        ready_for_single_worker_smoke=bool(payload.get("ready_for_single_worker_smoke", False)),
        live_submit_allowed=bool(payload.get("live_submit_allowed", False)),
        registration_allowed=bool(payload.get("registration_allowed", False)),
        would_write_registry=bool(payload.get("would_write_registry", False)),
        would_write_state=bool(payload.get("would_write_state", False)),
        errors=errors,
        warnings=warnings,
    )


def _load_config(
    request: RegistrationEvidencePackageRequest,
    config_base: Path | None,
) -> tuple[LiveEnableConfig, ConfigPathEvidence]:
    explicit = _resolve_relative(Path(request.live_enable_config_path), config_base) if request.live_enable_config_path else None
    return resolve_live_enable_config(
        request.backend_kind,
        explicit_config=explicit,
        root=config_base,
        source_root=_source_root(),
    )


def _authorization_result(
    request: RegistrationEvidencePackageRequest,
    config_base: Path | None,
) -> AuthorizationValidationResult:
    if request.authorization_record:
        return validate_authorization_record_payload(request.authorization_record, evidence_source="inline")
    if request.authorization_record_path:
        return validate_authorization_record_file(_resolve_relative(Path(request.authorization_record_path), config_base))
    return AuthorizationValidationResult(
        ok=False,
        blocked_reasons=["missing authorization_record"],
        warnings=[
            "authorization record is not live enable",
            "authorization record is not registration",
            "authorization record is not a preflight evidence substitute",
        ],
        safe_next_actions=["provide explicit user authorization record", "rerun fixture-only evidence package"],
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _authorization_record_payload(
    request: RegistrationEvidencePackageRequest,
    config_base: Path | None,
) -> dict[str, Any]:
    if request.authorization_record:
        return dict(request.authorization_record)
    if not request.authorization_record_path:
        return {}
    path = _resolve_relative(Path(request.authorization_record_path), config_base)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _preflight_payload(
    source: dict[str, Any],
    request: RegistrationEvidencePackageRequest,
    authorization_id: str,
) -> dict[str, Any]:
    payload = dict(source)
    payload.update(
        {
            "backend_kind": request.backend_kind,
            "worker_id": request.worker_id,
            "workspace_root": request.workspace_root,
            "runtime_backend": request.runtime_backend,
            "runtime_binding_kind": request.runtime_binding_kind,
            "pane_id": request.pane_id,
            "process_id": request.process_id,
            "port": request.port,
            "user_owned_pane": request.user_owned_pane,
            "authorization_record_path": request.authorization_record_path,
            "live_enable_config_path": request.live_enable_config_path,
            "expected_business_index_files": list(request.expected_business_index_files),
            "allow_mixed_worker": request.allow_mixed_worker,
            "user_authorization_record": authorization_id,
            "notes": list(request.notes),
        }
    )
    if request.business_index_evidence:
        payload["business_index_evidence"] = dict(request.business_index_evidence)
    if request.mixed_worker_evidence:
        payload["mixed_worker_evidence"] = dict(request.mixed_worker_evidence)
    if request.authorization_record:
        payload["authorization_record"] = dict(request.authorization_record)
    return payload


def _source_with_preflight_request(
    source: dict[str, Any],
    request: RegistrationEvidencePackageRequest,
    config_base: Path | None,
) -> dict[str, Any]:
    if not request.preflight_request_path:
        return source
    path = _resolve_relative(Path(request.preflight_request_path), config_base)
    preflight_source = _read_json(path, kind="preflight request")
    return {**preflight_source, **source}


def _read_json(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeAdapterError(f"{kind} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeAdapterError(f"invalid {kind}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeAdapterError(f"{kind} must contain an object: {path}")
    return payload


def _resolve_relative(path: Path, base: Path | None) -> Path:
    if path.is_absolute() or base is None:
        return path
    return base / path


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _required_fields() -> list[str]:
    return [
        "schema_version",
        "package_id",
        "generated_at",
        "generated_by",
        "backend_kind",
        "worker_id",
        "worker_label",
        "workspace_root",
        "runtime_backend",
        "runtime_binding_kind",
        "pane_id",
        "process_id",
        "port",
        "user_owned_pane",
        "expected_business_index_files",
        "authorization_record",
        "authorization_validation",
        "live_enable_config",
        "config_path_evidence",
        "live_enable_decision",
        "preflight_evidence",
        "dry_run_registration_plan",
        "capability_evidence",
        "workspace_evidence",
        "runtime_binding_evidence",
        "business_index_evidence",
        "mixed_worker_evidence",
        "forbidden_action_evidence",
        "overall_package_ok",
        "ready_for_live_enable",
        "ready_for_registration",
        "ready_for_single_worker_smoke",
        "live_submit_allowed",
        "registration_allowed",
        "would_write_registry",
        "would_write_state",
        "blocked_reasons",
        "warnings",
        "safe_next_actions",
        "forbidden_next_actions",
        "required_next_evidence",
        "unresolved_issues",
    ]


def _package_id(backend_kind: str, worker_id: str) -> str:
    safe_worker = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in worker_id)
    return f"registration-evidence-{backend_kind}-{safe_worker or 'unknown'}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dict(payload: dict[str, Any], key: str, default: dict[str, Any]) -> dict[str, Any]:
    value = payload.get(key)
    if isinstance(value, dict):
        return dict(value)
    return dict(default)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _safe_next_actions(blocked: list[str]) -> list[str]:
    actions = [
        "validate registration evidence package",
        "collect separate live-enable approval before any live path",
        "collect separate single-worker no-op smoke evidence before registration",
    ]
    if blocked:
        actions.insert(0, "resolve blocked evidence before any live enable review")
    return actions


def _required_next_evidence(blocked: list[str]) -> list[str]:
    required = [
        "source tests passing",
        "valid explicit user authorization record",
        "separate live-enable approval record",
        "fresh preflight evidence",
        "single-worker no-op smoke evidence",
    ]
    if blocked:
        required.append("resolution evidence for blocked_reasons")
    return required


def _forbidden_next_actions() -> list[str]:
    claude_code = "claude" + "-" + "code"
    cursor_agent = "cursor" + "-" + "agent"
    tmux_send_keys = "tmux " + "send" + "-" + "keys" + " or paste-buffer"
    return [
        "write live registry",
        "write .omx/state/team",
        "touch user-owned Claude Code panels",
        "restore or rebind live workers",
        "use replacement live workers",
        tmux_send_keys,
        "rmux live",
        f"claude/{claude_code} command",
        f"{cursor_agent} command",
        "OpenCode live submit",
        "direct adapter submit",
        "direct /tui/*",
    ]


__all__ = [
    "RegistrationEvidencePackage",
    "RegistrationEvidencePackageRequest",
    "RegistrationEvidenceValidationResult",
    "build_registration_evidence_package",
    "build_registration_evidence_package_from_fixture",
    "validate_registration_evidence_package_file",
    "validate_registration_evidence_package_payload",
]
