"""Registered worker records for future Claude Code worker integration.

This module validates local JSON only. A valid registered worker record is
inventory evidence for a future path; it is not authorization, live enable,
current liveness, or permission to submit a live task.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError


SUPPORTED_REGISTERED_BACKENDS = {"claude_code"}
KNOWN_BACKENDS = {"claude_code", "cursor_agent", "opencode_tui"}
TERMINAL_MARKERS = (".done", ".fail", ".blocked")


@dataclass(frozen=True)
class RegisteredWorkerRecord:
    schema_version: int
    backend_kind: str
    worker_id: str
    workspace_root: str
    runtime_backend: str
    runtime_binding_kind: str = ""
    pane_id: str = ""
    pane_pid: str = ""
    process_id: str = ""
    command: str = ""
    port: str = ""
    user_owned_pane: bool = False
    registration_status: str = "planned"
    live_enabled: bool = False
    allow_business_repo_writes: bool = False
    allow_mixed_worker: bool = False
    authorization_record: str = ""
    live_enable_config: str = ""
    smoke_evidence_path: str = ""
    explicit_business_repo_write_authorization: bool = False
    registered_single_worker_smoke_evidence: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegisteredWorkerRecord":
        return cls(
            schema_version=int(payload.get("schema_version", 0) or 0),
            backend_kind=str(payload.get("backend_kind", "")),
            worker_id=str(payload.get("worker_id", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            runtime_backend=str(payload.get("runtime_backend", "")),
            runtime_binding_kind=str(payload.get("runtime_binding_kind", "")),
            pane_id=str(payload.get("pane_id", "")),
            pane_pid=str(payload.get("pane_pid", "")),
            process_id=str(payload.get("process_id", "")),
            command=str(payload.get("command", "")),
            port=str(payload.get("port", "")),
            user_owned_pane=bool(payload.get("user_owned_pane", False)),
            registration_status=str(payload.get("registration_status", "planned")),
            live_enabled=bool(payload.get("live_enabled", False)),
            allow_business_repo_writes=bool(payload.get("allow_business_repo_writes", False)),
            allow_mixed_worker=bool(payload.get("allow_mixed_worker", False)),
            authorization_record=str(payload.get("authorization_record", "")),
            live_enable_config=str(payload.get("live_enable_config", "")),
            smoke_evidence_path=str(payload.get("smoke_evidence_path", "")),
            explicit_business_repo_write_authorization=bool(
                payload.get("explicit_business_repo_write_authorization", False)
            ),
            registered_single_worker_smoke_evidence=bool(payload.get("registered_single_worker_smoke_evidence", False)),
            notes=_string_list(payload.get("notes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerValidationResult:
    ok: bool
    backend_kind: str = ""
    worker_id: str = ""
    workspace_root: str = ""
    runtime_backend: str = ""
    pane_id: str = ""
    pane_pid: str = ""
    process_id: str = ""
    command: str = ""
    user_owned_pane: bool = False
    registration_status: str = ""
    live_enabled: bool = False
    allow_business_repo_writes: bool = False
    allow_mixed_worker: bool = False
    authorization_record_present: bool = False
    live_enable_config_present: bool = False
    smoke_evidence_present: bool = False
    business_ready: bool = False
    mixed_ready: bool = False
    live_submit_ready: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegisteredWorkerPlan:
    would_register: bool
    would_write_registry: bool
    would_write_state: bool
    registration_allowed: bool
    requires_explicit_allow_register: bool
    registry_path: str
    registry_record: dict[str, Any]
    validation_result: dict[str, Any]
    required_preflight_checks: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_registered_worker_record(
    *,
    backend_kind: str,
    worker_id: str,
    workspace_root: str,
    runtime_backend: str,
    runtime_binding_kind: str = "tmux-pane",
    pane_id: str = "",
    pane_pid: str = "",
    process_id: str = "",
    command: str = "",
    port: str = "",
    user_owned_pane: bool = False,
    registration_status: str = "planned",
    live_enabled: bool = False,
    allow_business_repo_writes: bool = False,
    allow_mixed_worker: bool = False,
    authorization_record: str = "",
    live_enable_config: str = "",
    smoke_evidence_path: str = "",
    notes: list[str] | None = None,
) -> RegisteredWorkerRecord:
    return RegisteredWorkerRecord(
        schema_version=1,
        backend_kind=backend_kind,
        worker_id=worker_id,
        workspace_root=workspace_root,
        runtime_backend=runtime_backend,
        runtime_binding_kind=runtime_binding_kind,
        pane_id=pane_id,
        pane_pid=pane_pid,
        process_id=process_id,
        command=command,
        port=port,
        user_owned_pane=user_owned_pane,
        registration_status=registration_status,
        live_enabled=live_enabled,
        allow_business_repo_writes=allow_business_repo_writes,
        allow_mixed_worker=allow_mixed_worker,
        authorization_record=authorization_record,
        live_enable_config=live_enable_config,
        smoke_evidence_path=smoke_evidence_path,
        notes=list(notes or []),
    )


def validate_registered_worker_record_file(path: Path | str) -> RegisteredWorkerValidationResult:
    record_path = Path(path)
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _invalid([f"registered worker record missing: {record_path}"], evidence_source=str(record_path))
    except json.JSONDecodeError as exc:
        return _invalid([f"malformed JSON: {exc.msg}"], evidence_source=str(record_path))
    if not isinstance(payload, dict):
        return _invalid(["registered worker record must contain an object"], evidence_source=str(record_path))
    return validate_registered_worker_record_payload(
        payload,
        evidence_source=str(record_path),
        base_path=record_path.parent,
    )


def validate_registered_worker_record_payload(
    payload: dict[str, Any],
    *,
    evidence_source: str = "inline",
    base_path: Path | None = None,
) -> RegisteredWorkerValidationResult:
    try:
        record = RegisteredWorkerRecord.from_dict(payload)
    except (TypeError, ValueError) as exc:
        return _invalid([f"registered worker fields malformed: {exc}"], evidence_source=evidence_source)

    blocked: list[str] = []
    warnings: list[str] = [
        "registered worker record is not authorization",
        "registered worker record is not live enable",
        "registered worker record is not current liveness",
        "valid registered worker record does not permit live submit",
    ]

    if record.backend_kind not in KNOWN_BACKENDS:
        blocked.append(f"unknown backend_kind: {record.backend_kind}")
    elif record.backend_kind not in SUPPORTED_REGISTERED_BACKENDS:
        blocked.append(f"{record.backend_kind} registered worker path is not supported")
    elif not record.authorization_record:
        blocked.append("missing authorization_record")
    if not record.worker_id:
        blocked.append("missing worker_id")
    if not record.workspace_root:
        blocked.append("missing workspace_root")
    elif not PurePosixPath(record.workspace_root).is_absolute():
        blocked.append("workspace_root must be absolute")
    if not record.runtime_backend:
        blocked.append("missing runtime_backend")
    if record.user_owned_pane and not record.authorization_record:
        blocked.append("user_owned_pane=true requires authorization_record")
    if record.live_enabled and not record.live_enable_config:
        blocked.append("live_enabled=true requires live_enable_config")
    if record.allow_business_repo_writes and not record.explicit_business_repo_write_authorization:
        blocked.append("allow_business_repo_writes=true requires explicit business repo write authorization")
    if record.allow_mixed_worker:
        blocked.append("allow_mixed_worker=true requires registered single-worker smoke evidence and separate mixed review")

    smoke = _smoke_evidence(record.smoke_evidence_path, base_path)
    if _status_claims_smoke(record.registration_status) and not smoke["present"]:
        blocked.append("smoke evidence missing report.md plus exactly one terminal marker")

    blocked = _dedupe(blocked)
    warnings = _dedupe(warnings)
    ok = not blocked
    return RegisteredWorkerValidationResult(
        ok=ok,
        backend_kind=record.backend_kind,
        worker_id=record.worker_id,
        workspace_root=record.workspace_root,
        runtime_backend=record.runtime_backend,
        pane_id=record.pane_id,
        pane_pid=record.pane_pid,
        process_id=record.process_id,
        command=record.command,
        user_owned_pane=record.user_owned_pane,
        registration_status=record.registration_status,
        live_enabled=record.live_enabled,
        allow_business_repo_writes=record.allow_business_repo_writes,
        allow_mixed_worker=record.allow_mixed_worker,
        authorization_record_present=bool(record.authorization_record),
        live_enable_config_present=bool(record.live_enable_config),
        smoke_evidence_present=bool(smoke["present"]),
        business_ready=False,
        mixed_ready=False,
        live_submit_ready=False,
        blocked_reasons=blocked,
        warnings=warnings,
        evidence=[
            {"source": "registered_worker_record", "detail": {"evidence_source": evidence_source}},
            {"source": "smoke_evidence", "detail": smoke},
        ],
        safe_next_actions=_safe_next_actions(ok),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def plan_registered_claude_worker(
    record: RegisteredWorkerRecord | dict[str, Any],
    *,
    registry_path: str = ".omx/framework/team/registered-workers.json",
    base_path: Path | None = None,
    allow_register: bool = False,
) -> RegisteredWorkerPlan:
    candidate = record if isinstance(record, RegisteredWorkerRecord) else RegisteredWorkerRecord.from_dict(record)
    validation = validate_registered_worker_record_payload(
        candidate.to_dict(),
        evidence_source="registered-worker-plan",
        base_path=base_path,
    )
    warnings = _dedupe(
        validation.warnings
        + [
            "registered-worker-plan is dry-run only",
            "registered-worker-plan does not write registry or runtime state",
            "registered-worker-plan does not call Claude Code, tmux, OpenCode, or rmux",
        ]
    )
    blocked = list(validation.blocked_reasons)
    if not allow_register:
        blocked.append("explicit allow-register was not provided")
    blocked = _dedupe(blocked)
    return RegisteredWorkerPlan(
        would_register=False,
        would_write_registry=False,
        would_write_state=False,
        registration_allowed=False,
        requires_explicit_allow_register=True,
        registry_path=registry_path,
        registry_record=candidate.to_dict(),
        validation_result=validation.to_dict(),
        required_preflight_checks=[
            "source tests pass",
            "validate registered worker record",
            "registration evidence package valid",
            "single-worker registered no-op smoke fixture passes",
            "no active locks",
            "watcher alerts reviewed",
            "board validate reviewed",
            "business repo index unchanged",
        ],
        required_artifacts=[
            "registered worker record JSON",
            "authorization record JSON",
            "live enable config JSON",
            "smoke report.md",
            "exactly one of .done/.fail/.blocked",
        ],
        blocked_reasons=blocked,
        warnings=warnings,
        safe_next_actions=validation.safe_next_actions,
        forbidden_next_actions=validation.forbidden_next_actions,
    )


def _smoke_evidence(value: str, base_path: Path | None) -> dict[str, Any]:
    if not value:
        return {"path": "", "report_md": False, "terminal_markers": [], "present": False}
    path = Path(value)
    if not path.is_absolute() and base_path is not None:
        path = base_path / path
    markers = [marker for marker in TERMINAL_MARKERS if (path / marker).is_file()]
    report_md = (path / "report.md").is_file() and (path / "report.md").read_text(encoding="utf-8", errors="replace").strip()
    return {
        "path": str(path),
        "report_md": bool(report_md),
        "terminal_markers": markers,
        "present": bool(report_md) and len(markers) == 1,
    }


def _status_claims_smoke(status: str) -> bool:
    normalized = status.lower().replace("-", "_")
    return "smoke" in normalized or normalized in {"validated", "adapter_smoke_passed"}


def _invalid(blocked: list[str], *, evidence_source: str) -> RegisteredWorkerValidationResult:
    return RegisteredWorkerValidationResult(
        ok=False,
        blocked_reasons=_dedupe(blocked),
        warnings=[
            "registered worker record is not authorization",
            "registered worker record is not live enable",
            "registered worker record is not current liveness",
            "valid registered worker record does not permit live submit",
        ],
        evidence=[{"source": "registered_worker_record", "detail": {"evidence_source": evidence_source}}],
        safe_next_actions=_safe_next_actions(False),
        forbidden_next_actions=_forbidden_next_actions(),
    )


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


def _safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return [
            "run registered-worker-plan as dry-run",
            "collect registered single-worker no-op smoke evidence before any live enable",
            "do not register or submit live workers",
        ]
    return [
        "fix registered worker record fields",
        "rerun validate-registered-worker-record",
        "do not register or submit live workers",
    ]


def _forbidden_next_actions() -> list[str]:
    return [
        "write live registry",
        "write runtime state",
        "touch user-owned Claude Code panel",
        "restore or rebind live workers",
        "use replacement live workers",
        "invoke claude or claude-code",
        "submit OpenCode live",
        "tmux send-keys",
        "tmux paste-buffer",
        "direct adapter submit",
        "direct prompt mutation endpoints",
        "business repo writes",
        "mixed OpenCode plus Claude execution",
    ]


def write_record_under_root(root: Path, output: Path, record: RegisteredWorkerRecord) -> None:
    output_path = output.resolve()
    try:
        output_path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeAdapterError(f"output must stay under source root: {output_path}") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "RegisteredWorkerPlan",
    "RegisteredWorkerRecord",
    "RegisteredWorkerValidationResult",
    "build_registered_worker_record",
    "plan_registered_claude_worker",
    "validate_registered_worker_record_file",
    "validate_registered_worker_record_payload",
    "write_record_under_root",
]
