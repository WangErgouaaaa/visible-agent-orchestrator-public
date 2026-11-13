"""Dry-run preflight evidence for future live worker registration.

The fixture path is pure JSON evaluation. It does not contact live workers,
write registries, or inspect the live runtime.
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
from .backends import available_backends
from .live_enable import (
    LiveEnableConfig,
    backend_supports_live_submit,
    evaluate_live_enable,
    public_backend_kind,
)
from .registration import DryRunRegistrationRequest, build_dry_run_registration_plan


@dataclass(frozen=True)
class DryRunPreflightRequest:
    backend_kind: str
    worker_id: str = ""
    workspace_root: str = ""
    runtime_backend: str = ""
    runtime_binding_kind: str = ""
    pane_id: str = ""
    process_id: str = ""
    port: str = ""
    user_owned_pane: bool = False
    user_authorization_record: str = ""
    authorization_record_path: str = ""
    authorization_record: dict[str, Any] = field(default_factory=dict)
    live_enable_config_path: str = ""
    allowed_workspace_roots: list[str] = field(default_factory=list)
    allowed_read_roots: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    expected_business_index_files: list[str] = field(default_factory=list)
    require_no_active_locks: bool = True
    require_watcher_alerts_zero: bool = True
    require_business_index_unchanged: bool = True
    require_health_ok: bool = True
    require_policy_ok: bool = True
    require_doctor_ok: bool = True
    require_single_worker_only: bool = True
    allow_mixed_worker: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DryRunPreflightRequest":
        return cls(
            backend_kind=str(payload.get("backend_kind", "")),
            worker_id=str(payload.get("worker_id", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            runtime_backend=str(payload.get("runtime_backend", "")),
            runtime_binding_kind=str(payload.get("runtime_binding_kind", "")),
            pane_id=str(payload.get("pane_id", "")),
            process_id=str(payload.get("process_id", "")),
            port=str(payload.get("port", "")),
            user_owned_pane=bool(payload.get("user_owned_pane", False)),
            user_authorization_record=str(payload.get("user_authorization_record", "")),
            authorization_record_path=str(payload.get("authorization_record_path", "")),
            authorization_record=_dict(payload, "authorization_record", {}),
            live_enable_config_path=str(payload.get("live_enable_config_path", "")),
            allowed_workspace_roots=_string_list(payload.get("allowed_workspace_roots", [])),
            allowed_read_roots=_string_list(payload.get("allowed_read_roots", [])),
            allowed_write_roots=_string_list(payload.get("allowed_write_roots", [])),
            expected_business_index_files=_string_list(payload.get("expected_business_index_files", [])),
            require_no_active_locks=bool(payload.get("require_no_active_locks", True)),
            require_watcher_alerts_zero=bool(payload.get("require_watcher_alerts_zero", True)),
            require_business_index_unchanged=bool(payload.get("require_business_index_unchanged", True)),
            require_health_ok=bool(payload.get("require_health_ok", True)),
            require_policy_ok=bool(payload.get("require_policy_ok", True)),
            require_doctor_ok=bool(payload.get("require_doctor_ok", True)),
            require_single_worker_only=bool(payload.get("require_single_worker_only", True)),
            allow_mixed_worker=bool(payload.get("allow_mixed_worker", False)),
            notes=_string_list(payload.get("notes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DryRunPreflightEvidence:
    backend_kind: str
    worker_id: str
    workspace_root: str
    runtime_backend: str
    collected_at: str
    evidence_source: str
    capability_evidence: dict[str, Any]
    live_enable_decision: dict[str, Any]
    dry_run_registration_plan: dict[str, Any]
    workspace_evidence: dict[str, Any]
    authorization_evidence: dict[str, Any]
    runtime_health_evidence: dict[str, Any]
    lock_evidence: dict[str, Any]
    watcher_evidence: dict[str, Any]
    business_index_evidence: dict[str, Any]
    mixed_worker_evidence: dict[str, Any]
    forbidden_action_evidence: dict[str, Any]
    authorization_record_present: bool
    authorization_record_valid: bool
    authorization_id: str
    authorization_scope: str
    authorization_blocks: list[str]
    authorization_warnings: list[str]
    authorization_safe_next_actions: list[str]
    authorization_forbidden_next_actions: list[str]
    overall_preflight_ok: bool
    live_submit_allowed: bool
    registration_allowed: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_next_evidence: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect_preflight_evidence_from_fixture(
    path: Path | str,
    *,
    authorization_record_path: Path | str | None = None,
) -> DryRunPreflightEvidence:
    fixture_path = Path(path)
    payload = _read_json(fixture_path)
    if authorization_record_path is not None:
        payload["authorization_record_path"] = str(authorization_record_path)
    return collect_preflight_evidence_from_payload(payload, evidence_source="fixture", config_base=fixture_path.parent)


def collect_preflight_evidence_from_payload(
    payload: dict[str, Any],
    *,
    evidence_source: str = "fixture",
    config_base: Path | None = None,
) -> DryRunPreflightEvidence:
    request = DryRunPreflightRequest.from_dict(payload)
    return evaluate_preflight_evidence(
        request,
        payload=payload,
        evidence_source=evidence_source,
        config_base=config_base,
    )


def evaluate_preflight_evidence(
    request: DryRunPreflightRequest,
    *,
    payload: dict[str, Any] | None = None,
    evidence_source: str = "fixture",
    config_base: Path | None = None,
) -> DryRunPreflightEvidence:
    public = public_backend_kind(request.backend_kind)
    source = payload or {}
    config = _live_config_from_payload_or_default(source, request, config_base)
    authorization_result = _authorization_result(request, config_base)
    effective_authorization_id = request.user_authorization_record
    if authorization_result.ok:
        effective_authorization_id = authorization_result.authorization_id
    if effective_authorization_id and not config.user_authorization_record:
        config = LiveEnableConfig(**{**config.to_dict(), "user_authorization_record": effective_authorization_id})

    decision = evaluate_live_enable(
        backend_kind=public,
        config=config,
        workspace_root=request.workspace_root,
        worker_id=request.worker_id,
        runtime_backend=request.runtime_backend,
        user_owned_pane=request.user_owned_pane,
        config_source=request.live_enable_config_path or evidence_source,
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
            allowed_write_roots=request.allowed_write_roots,
            allowed_read_roots=request.allowed_read_roots,
        ),
        config=config,
        config_source=request.live_enable_config_path or evidence_source,
    )

    capability = _capability_evidence(public)
    workspace = _dict(source, "workspace_evidence", {"workspace_root": request.workspace_root})
    authorization = {
        **_dict(source, "authorization_evidence", {}),
        "user_authorization_record_present": bool(effective_authorization_id or config.user_authorization_record),
        "user_owned_pane": request.user_owned_pane,
        "authorization_record_present": _authorization_present(request),
        "authorization_record_valid": authorization_result.ok,
        "authorization_id": authorization_result.authorization_id,
        "authorization_scope": authorization_result.authorization_scope,
    }
    runtime_health = _dict(
        source,
        "runtime_health_evidence",
        {"health_ok": True, "policy_ok": True, "doctor_ok": True},
    )
    locks = _dict(source, "lock_evidence", {"active_nonterminal_locks": 0})
    watcher = _dict(source, "watcher_evidence", {"active_alerts": 0})
    business = _business_index_evidence(source, request)
    mixed = _dict(source, "mixed_worker_evidence", {"mixed_worker_requested": False})
    forbidden = {
        "would_call_live_worker": False,
        "would_write_live_registry": False,
        "would_write_state_team": False,
        "direct_submit_allowed": False,
    }

    blocked = _dedupe(decision.blocked_reasons + registration.blocked_reasons)
    if request.user_owned_pane:
        blocked.append("user-owned pane fail closed")
    if public in {"claude_code", "cursor_agent"} and not authorization["user_authorization_record_present"]:
        blocked.append(f"{public} missing user_authorization_record")
    if public in {"claude_code", "cursor_agent"} and not authorization["authorization_record_present"]:
        blocked.append(f"{public} missing authorization_record")
    if authorization["authorization_record_present"] and not authorization_result.ok:
        blocked.append("authorization record invalid")
    if authorization_result.ok and authorization_result.backend_kind != public:
        blocked.append(f"authorization backend mismatch: {authorization_result.backend_kind} != {public}")
    if request.require_no_active_locks and int(locks.get("active_nonterminal_locks", locks.get("active_locks", 0)) or 0) > 0:
        blocked.append("active nonterminal lock present")
    if request.require_watcher_alerts_zero and int(watcher.get("active_alerts", 0) or 0) != 0:
        blocked.append("watcher alerts nonzero")
    if request.require_health_ok and not bool(runtime_health.get("health_ok", True)):
        blocked.append("health not ok")
    if request.require_policy_ok and not bool(runtime_health.get("policy_ok", True)):
        blocked.append("policy not ok")
    if request.require_doctor_ok and not bool(runtime_health.get("doctor_ok", True)):
        blocked.append("doctor not ok")
    if request.require_business_index_unchanged and not bool(business.get("unchanged", True)):
        blocked.append("business index changed")
    if bool(mixed.get("mixed_worker_requested", False)) and not request.allow_mixed_worker:
        blocked.append("mixed worker requested while allow_mixed_worker=false")

    blocked = _dedupe(blocked)
    warnings = _dedupe(
        decision.warnings
        + registration.warnings
        + authorization_result.warnings
        + [
            "preflight evidence is not authorization",
            "preflight evidence is not registration",
            "preflight evidence does not call live workers",
        ]
    )
    if public == "opencode_tui":
        warnings.append("opencode_tui remains wrapper-only for live submit")

    overall_ok = not blocked
    live_submit_allowed = overall_ok and bool(decision.live_enabled) and bool(decision.capability_supports_live_submit)
    registration_allowed = False
    return DryRunPreflightEvidence(
        backend_kind=public,
        worker_id=request.worker_id,
        workspace_root=request.workspace_root,
        runtime_backend=request.runtime_backend,
        collected_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        evidence_source=evidence_source,
        capability_evidence=capability,
        live_enable_decision=decision.to_dict(),
        dry_run_registration_plan=registration.to_dict(),
        workspace_evidence=workspace,
        authorization_evidence=authorization,
        runtime_health_evidence=runtime_health,
        lock_evidence=locks,
        watcher_evidence=watcher,
        business_index_evidence=business,
        mixed_worker_evidence=mixed,
        forbidden_action_evidence=forbidden,
        authorization_record_present=authorization["authorization_record_present"],
        authorization_record_valid=authorization["authorization_record_valid"],
        authorization_id=authorization["authorization_id"],
        authorization_scope=authorization["authorization_scope"],
        authorization_blocks=authorization_result.blocked_reasons,
        authorization_warnings=authorization_result.warnings,
        authorization_safe_next_actions=authorization_result.safe_next_actions,
        authorization_forbidden_next_actions=authorization_result.forbidden_next_actions,
        overall_preflight_ok=overall_ok,
        live_submit_allowed=live_submit_allowed,
        registration_allowed=registration_allowed,
        blocked_reasons=blocked,
        warnings=warnings,
        required_next_evidence=_required_next_evidence(blocked),
        safe_next_actions=_safe_next_actions(overall_ok, public),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeAdapterError(f"preflight request missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeAdapterError(f"invalid preflight request: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeAdapterError(f"preflight request must contain an object: {path}")
    return payload


def _live_config_from_payload_or_default(
    payload: dict[str, Any],
    request: DryRunPreflightRequest,
    config_base: Path | None,
) -> LiveEnableConfig:
    inline = payload.get("live_enable_config")
    if isinstance(inline, dict):
        return LiveEnableConfig.from_dict(inline, request.backend_kind)
    if request.live_enable_config_path:
        path = Path(request.live_enable_config_path)
        if not path.is_absolute() and config_base is not None:
            path = config_base / path
        return LiveEnableConfig.from_dict(_read_json(path), request.backend_kind)
    return LiveEnableConfig.disabled_default(request.backend_kind)


def _capability_evidence(public: str) -> dict[str, Any]:
    canonical = ""
    for item in available_backends():
        if item["agent_kind"].replace("-", "_") == public.replace("_code", "_code") or item["name"].startswith(public.replace("_", "-")):
            canonical = item["name"]
    supports = backend_supports_live_submit(public)
    return {
        "backend_kind": public,
        "canonical_backend": canonical,
        "supports_live_submit": supports,
        "direct_submit_allowed": False,
        "live_submit_requires_visible_dispatch_wrapper": public == "opencode_tui",
    }


def _business_index_evidence(payload: dict[str, Any], request: DryRunPreflightRequest) -> dict[str, Any]:
    evidence = _dict(payload, "business_index_evidence", {})
    expected = request.expected_business_index_files
    actual = _string_list(evidence.get("actual_index_files", expected))
    unchanged = True
    if expected:
        unchanged = sorted(expected) == sorted(actual)
    return {
        "expected_index_files": expected,
        "actual_index_files": actual,
        "unchanged": unchanged,
    }


def _authorization_result(
    request: DryRunPreflightRequest,
    config_base: Path | None,
) -> AuthorizationValidationResult:
    if request.authorization_record:
        return validate_authorization_record_payload(request.authorization_record, evidence_source="inline")
    if request.authorization_record_path:
        path = Path(request.authorization_record_path)
        if not path.is_absolute() and config_base is not None:
            path = config_base / path
        return validate_authorization_record_file(path)
    return AuthorizationValidationResult(
        ok=False,
        blocked_reasons=["missing authorization_record"],
        warnings=[
            "authorization record is not live enable",
            "authorization record is not registration",
            "authorization record is not a preflight evidence substitute",
        ],
        safe_next_actions=[
            "provide explicit user authorization record",
            "rerun fixture-only preflight evidence",
            "do not register or submit live workers",
        ],
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _authorization_present(request: DryRunPreflightRequest) -> bool:
    return bool(request.authorization_record or request.authorization_record_path)


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


def _required_next_evidence(blocked: list[str]) -> list[str]:
    if not blocked:
        return ["separate live authorization remains required before any live smoke"]
    return [
        "source tests passing",
        "explicit user authorization record",
        "no active locks evidence",
        "watcher alerts zero evidence",
        "business index unchanged evidence",
    ]


def _safe_next_actions(overall_ok: bool, backend_kind: str) -> list[str]:
    if overall_ok and backend_kind == "opencode_tui":
        return ["continue only through existing visible dispatch wrapper with explicit authorization"]
    return [
        "rerun fixture-only preflight evidence",
        "prepare separate authorization record format",
        "do not register or submit live workers",
    ]


def _forbidden_next_actions() -> list[str]:
    return [
        "write live registry",
        "write .omx/state/team",
        "touch user-owned Claude Code panels",
        "restore or rebind live workers",
        "use replacement live workers",
        "tmux send-keys or paste-buffer",
        "rmux live",
        "claude/claude-code command",
        "cursor-agent command",
        "OpenCode live submit",
        "direct adapter submit",
        "direct /tui/*",
    ]


__all__ = [
    "DryRunPreflightEvidence",
    "DryRunPreflightRequest",
    "collect_preflight_evidence_from_fixture",
    "collect_preflight_evidence_from_payload",
    "evaluate_preflight_evidence",
]
