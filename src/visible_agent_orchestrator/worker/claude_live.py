"""Gated Claude Code visible-pane live adapter model.

This module models the dedicated Claude visible-pane submit path. It defaults
closed and only reaches tmux execution when explicit real-submit and tmux-input
gates, authorization, live-enable config, and scope checks all pass.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError
from visible_agent_orchestrator.runtime_paths import workspace_root

from .authorization import (
    UserAuthorizationRecord,
    validate_authorization_record_file,
)
from .live_enable import LiveEnableConfig, backend_supports_live_submit, load_live_enable_config


BUSINESS_REPO_PATH = str(workspace_root())
SUBMIT_METHOD = "claude-visible-pane-tmux-reference"
SMOKE_ARTIFACT_ROOT_FRAGMENT = "/.omx/framework/team/claude-live-smoke/"
DEFAULT_TERMINAL_MARKERS = ["done", "fail", "blocked"]
MAX_PROMPT_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class ClaudeLiveSubmitRequest:
    backend_kind: str = "claude_code"
    worker_id: str = ""
    workspace_root: str = ""
    runtime_backend: str = ""
    runtime_binding_kind: str = "tmux-pane"
    pane_id: str = ""
    pane_pid: str = ""
    process_id: str = ""
    command: str = ""
    user_owned_pane: bool = False
    task_dir: str = ""
    task_id: str = ""
    prompt_text: str = ""
    prompt_file: str = ""
    authorization_record_path: str = ""
    live_enable_config_path: str = ""
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    report_path: str = ""
    terminal_markers: list[str] = field(default_factory=lambda: list(DEFAULT_TERMINAL_MARKERS))
    allow_real: bool = False
    allow_tmux_input: bool = False
    allow_user_owned_pane: bool = False
    require_report_marker_protocol: bool = True
    active_lock_count: int = 0
    active_lock_evidence_source: str = ""
    dry_run: bool = True
    created_at: str = ""
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClaudeLiveSubmitRequest":
        return cls(
            backend_kind=str(payload.get("backend_kind", "claude_code")),
            worker_id=str(payload.get("worker_id", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            runtime_backend=str(payload.get("runtime_backend", "")),
            runtime_binding_kind=str(payload.get("runtime_binding_kind", "tmux-pane")),
            pane_id=str(payload.get("pane_id", "")),
            pane_pid=str(payload.get("pane_pid", "")),
            process_id=str(payload.get("process_id", "")),
            command=str(payload.get("command", "")),
            user_owned_pane=bool(payload.get("user_owned_pane", False)),
            task_dir=str(payload.get("task_dir", "")),
            task_id=str(payload.get("task_id", "")),
            prompt_text=str(payload.get("prompt_text", "")),
            prompt_file=str(payload.get("prompt_file", "")),
            authorization_record_path=str(payload.get("authorization_record_path", "")),
            live_enable_config_path=str(payload.get("live_enable_config_path", "")),
            allowed_write_roots=_string_list(payload.get("allowed_write_roots", [])),
            forbidden_paths=_string_list(payload.get("forbidden_paths", [])),
            expected_outputs=_string_list(payload.get("expected_outputs", [])),
            report_path=str(payload.get("report_path", "")),
            terminal_markers=_string_list(payload.get("terminal_markers", DEFAULT_TERMINAL_MARKERS)),
            allow_real=bool(payload.get("allow_real", False)),
            allow_tmux_input=bool(payload.get("allow_tmux_input", False)),
            allow_user_owned_pane=bool(payload.get("allow_user_owned_pane", False)),
            require_report_marker_protocol=bool(payload.get("require_report_marker_protocol", True)),
            active_lock_count=int(payload.get("active_lock_count", 0) or 0),
            active_lock_evidence_source=str(payload.get("active_lock_evidence_source", "")),
            dry_run=bool(payload.get("dry_run", True)),
            created_at=str(payload.get("created_at", "")),
            notes=_string_list(payload.get("notes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaudeLiveDeliveryEnvelope:
    worker_id: str
    task_id: str
    task_dir: str
    workspace_root: str
    pane_id: str
    pane_pid: str
    process_id: str
    command: str
    prompt_source: str
    prompt_path: str
    prompt_bytes: int
    prompt_sha256: str
    prompt_present: bool
    prompt_file_read: bool
    prompt_read_error: str
    prompt_gate_ok: bool
    authorization_record_path: str
    live_enable_config_path: str
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    report_path: str = ""
    terminal_markers: list[str] = field(default_factory=lambda: list(DEFAULT_TERMINAL_MARKERS))
    created_at: str = ""
    prompt_gate_checked_forbidden_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaudeLiveSubmitExecutionDecision:
    ok_to_execute: bool
    allow_real: bool
    allow_tmux_input: bool
    live_enabled: bool
    adapter_available: bool
    authorization_ok: bool
    authorization_not_expired: bool
    user_owned_pane_allowed: bool
    workspace_allowed: bool
    business_writes_forbidden: bool
    task_dir_allowed: bool
    report_marker_protocol_required: bool
    no_active_locks_required: bool = False
    active_locks_clear: bool = True
    active_lock_count: int = 0
    active_lock_evidence_source: str = ""
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaudeTmuxSubmitCommandPlan:
    would_execute: bool
    command_sequence: list[dict[str, Any]] = field(default_factory=list)
    uses_tmux_load_buffer: bool = False
    uses_tmux_paste_buffer: bool = False
    uses_tmux_send_enter: bool = False
    uses_tmux_delete_buffer: bool = False
    buffer_name: str = ""
    prompt_file: str = ""
    prompt_source: str = ""
    prompt_bytes: int = 0
    prompt_sha256: str = ""
    prompt_present: bool = False
    prompt_file_read: bool = False
    prompt_read_error: str = ""
    prompt_gate_ok: bool = False
    prompt_gate_checked_forbidden_paths: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok_to_execute(self) -> bool:
        return self.would_execute

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ok_to_execute"] = self.ok_to_execute
        return payload


@dataclass(frozen=True)
class ClaudeLiveSubmitPlan:
    backend_kind: str
    worker_id: str
    would_submit: bool
    submit_method: str
    requires_tmux_input: bool
    tmux_input_allowed: bool
    user_owned_pane_allowed: bool
    authorization_ok: bool
    live_enabled: bool
    supports_live_submit: bool
    live_submit_adapter_available: bool
    eligible: bool
    registration_allowed: bool
    live_submit_allowed: bool
    dry_run: bool = True
    executed: bool = False
    delivery_envelope: dict[str, Any] = field(default_factory=dict)
    execution_decision: dict[str, Any] = field(default_factory=dict)
    tmux_command_plan: dict[str, Any] = field(default_factory=dict)
    required_preflight_checks: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaudeLiveSubmitResult:
    attempted: bool
    submitted: bool
    task_id: str
    task_dir: str
    executed: bool = False
    delivery_envelope: dict[str, Any] = field(default_factory=dict)
    execution_decision: dict[str, Any] = field(default_factory=dict)
    tmux_command_plan: dict[str, Any] = field(default_factory=dict)
    delivery_artifacts: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedPromptContent:
    text: str = ""
    source: str = "missing"
    path: str = ""
    bytes: int = 0
    sha256: str = ""
    read_error: str = ""
    present: bool = False
    file_read: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "prompt_source": self.source,
            "prompt_path": self.path,
            "prompt_bytes": self.bytes,
            "prompt_sha256": self.sha256,
            "prompt_present": self.present,
            "prompt_file_read": self.file_read,
            "prompt_read_error": self.read_error,
        }


def build_claude_live_submit_plan(request: ClaudeLiveSubmitRequest) -> ClaudeLiveSubmitPlan:
    config, config_source = _load_config(request)
    authorization, auth_payload = _load_authorization(request.authorization_record_path)
    authorization_ok = bool(authorization.get("ok", False))
    record = _record_from_payload(auth_payload) if auth_payload else None
    live_enabled = bool(config.live_enabled)
    technical_support = backend_supports_live_submit("claude_code")
    adapter_available = True
    resolved_prompt = resolve_prompt_content(request)
    prompt_gate_ok, prompt_gate_checked_forbidden_paths = _prompt_gate_result(request, record, resolved_prompt)
    envelope = build_claude_live_delivery_envelope(request, record, resolved_prompt, prompt_gate_ok, prompt_gate_checked_forbidden_paths)

    blocked: list[str] = []
    warnings: list[str] = [
        "Claude live adapter model is source-only in this release; adapter implementation is not live enable",
        "no tmux input is executed by claude-live-plan",
    ]
    evidence: list[dict[str, Any]] = [
        {
            "source": "claude_live_submit_request",
            "detail": {
                "worker_id": request.worker_id,
                "workspace_root": request.workspace_root,
                "runtime_backend": request.runtime_backend,
                "runtime_binding_kind": request.runtime_binding_kind,
                "pane_id": request.pane_id,
                "pane_pid": request.pane_pid,
                "process_id": request.process_id,
                "command": request.command,
                "user_owned_pane": request.user_owned_pane,
                "allow_real": request.allow_real,
                "allow_tmux_input": request.allow_tmux_input,
                "active_lock_count": request.active_lock_count,
                "active_lock_evidence_source": request.active_lock_evidence_source,
                **resolved_prompt.metadata(),
            },
        },
        {
            "source": "live_enable_config",
            "detail": {"config_source": config_source, **config.to_dict()},
        },
        {
            "source": "authorization_record",
            "detail": authorization,
        },
    ]

    if request.backend_kind != "claude_code":
        blocked.append(f"unsupported backend for Claude live adapter: {request.backend_kind}")
    if not technical_support:
        blocked.append("claude_code capability supports_live_submit=false")
    if not live_enabled:
        blocked.append("disabled live gate: claude_code live_enabled=false")
    if not request.allow_real:
        blocked.append("allow_real=false")
    if not request.allow_tmux_input:
        blocked.append("allow_tmux_input=false")
    if not request.require_report_marker_protocol:
        blocked.append("require_report_marker_protocol=false")
    if config.require_no_active_locks and request.active_lock_count > 0:
        blocked.append(f"active locks > 0: {request.active_lock_count}")
    if not resolved_prompt.present:
        blocked.append(resolved_prompt.read_error or "prompt missing")
    authorization_not_expired = "authorization expired" not in authorization.get("blocked_reasons", [])
    if not authorization_ok:
        blocked.extend(authorization.get("blocked_reasons", []) or ["authorization invalid or missing"])
    if record is not None:
        blocked.extend(_record_mismatch_blocks(request, record))
    if request.user_owned_pane and not _user_owned_pane_allowed(request, record):
        blocked.append("user-owned pane requires explicit allow_user_owned_pane")
    if _scope_blocks_business_write(request, record):
        blocked.append("business repo write scope requires allow_business_repo_writes=true")
    if not _task_dir_allowed(request, record):
        blocked.append("task_dir is outside allowed smoke artifact or allowed_write_roots")
    if not prompt_gate_ok:
        blocked.append("prompt must explicitly forbid business repo writes when business repo is forbidden")
    blocked.extend(_config_scope_blocks(request, config))

    if request.user_owned_pane and _user_owned_pane_allowed(request, record):
        warnings.append("user-owned pane is explicitly authorized for this plan, but live gates still apply")
    if not config.allow_mixed_worker:
        warnings.append("mixed worker execution is not allowed")

    blocked = _dedupe(blocked)
    warnings = _dedupe(warnings)
    decision = ClaudeLiveSubmitExecutionDecision(
        ok_to_execute=not blocked,
        allow_real=request.allow_real,
        allow_tmux_input=request.allow_tmux_input,
        live_enabled=live_enabled,
        adapter_available=adapter_available,
        authorization_ok=authorization_ok,
        authorization_not_expired=authorization_not_expired,
        user_owned_pane_allowed=_user_owned_pane_allowed(request, record),
        workspace_allowed=_workspace_allowed(request, config),
        business_writes_forbidden=not _scope_blocks_business_write(request, record),
        task_dir_allowed=_task_dir_allowed(request, record),
        report_marker_protocol_required=request.require_report_marker_protocol,
        no_active_locks_required=config.require_no_active_locks,
        active_locks_clear=request.active_lock_count == 0,
        active_lock_count=request.active_lock_count,
        active_lock_evidence_source=request.active_lock_evidence_source,
        blocked_reasons=blocked,
        warnings=warnings,
    )
    command_plan = build_claude_tmux_submit_command_plan(request, envelope, decision, resolved_prompt)
    would_submit = decision.ok_to_execute
    return ClaudeLiveSubmitPlan(
        backend_kind="claude_code",
        worker_id=request.worker_id,
        would_submit=would_submit,
        submit_method=SUBMIT_METHOD,
        requires_tmux_input=True,
        tmux_input_allowed=request.allow_tmux_input,
        user_owned_pane_allowed=_user_owned_pane_allowed(request, record),
        authorization_ok=authorization_ok,
        live_enabled=live_enabled,
        supports_live_submit=technical_support,
        live_submit_adapter_available=adapter_available,
        eligible=would_submit,
        registration_allowed=would_submit and bool(record and record.allow_registration),
        live_submit_allowed=would_submit,
        dry_run=request.dry_run,
        executed=False,
        delivery_envelope=envelope.to_dict(),
        execution_decision=decision.to_dict(),
        tmux_command_plan=command_plan.to_dict(),
        required_preflight_checks=[
            "live-enable-status",
            "check-live-eligibility",
            "dry-run-register",
            "preflight-evidence",
            "business-index-unchanged",
            *(
                ["active-locks-zero"]
                if config.require_no_active_locks
                else []
            ),
            "exact-pane-process-workspace-match",
        ],
        required_artifacts=[".received", ".started", "report.md", "exactly one of .done/.fail/.blocked"],
        expected_outputs=["report.md", ".done/.fail/.blocked"],
        forbidden_actions=[
            "raw tmux send-keys",
            "raw tmux paste-buffer",
            "claude/claude-code command invocation",
            "OpenCode live submit",
            "cursor-agent command invocation",
            "direct adapter submit",
            "direct /tui/*",
            "rmux live",
            "business repo writes",
            "mixed worker execution",
            "write live registry",
            "write .omx/state/team",
        ],
        blocked_reasons=blocked,
        warnings=warnings,
        evidence=evidence
        + [
            {"source": "claude_live_delivery_envelope", "detail": envelope.to_dict()},
            {"source": "claude_live_execution_decision", "detail": decision.to_dict()},
            {"source": "claude_tmux_submit_command_plan", "detail": command_plan.to_dict()},
        ],
    )


def build_claude_live_submit_result(request: ClaudeLiveSubmitRequest) -> ClaudeLiveSubmitResult:
    plan = build_claude_live_submit_plan(request)
    command_plan_payload = dict(plan.tmux_command_plan)
    command_plan_payload.pop("ok_to_execute", None)
    command_plan = ClaudeTmuxSubmitCommandPlan(**command_plan_payload)
    result = execute_claude_tmux_submit(
        command_plan,
        allow_real=request.allow_real,
        allow_tmux_input=request.allow_tmux_input,
    )
    return replace(
        result,
        delivery_envelope=plan.delivery_envelope,
        execution_decision=plan.execution_decision,
        evidence=plan.evidence + result.evidence,
    )


def build_claude_live_delivery_envelope(
    request: ClaudeLiveSubmitRequest,
    record: UserAuthorizationRecord | None = None,
    resolved_prompt: ResolvedPromptContent | None = None,
    prompt_gate_ok: bool | None = None,
    prompt_gate_checked_forbidden_paths: list[str] | None = None,
) -> ClaudeLiveDeliveryEnvelope:
    resolved_prompt = resolved_prompt or resolve_prompt_content(request)
    if prompt_gate_ok is None:
        prompt_gate_ok, prompt_gate_checked_forbidden_paths = _prompt_gate_result(request, record, resolved_prompt)
    prompt_gate_checked_forbidden_paths = prompt_gate_checked_forbidden_paths or []
    forbidden_paths = _dedupe(list(request.forbidden_paths) + (list(record.forbidden_paths) if record else []))
    allowed_write_roots = _dedupe(list(request.allowed_write_roots) + (list(record.allowed_write_roots) if record else []))
    expected_outputs = request.expected_outputs or ["report.md", ".done/.fail/.blocked"]
    report_path = request.report_path or (str(PurePosixPath(request.task_dir) / "report.md") if request.task_dir else "")
    created_at = request.created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return ClaudeLiveDeliveryEnvelope(
        worker_id=request.worker_id,
        task_id=request.task_id,
        task_dir=request.task_dir,
        workspace_root=request.workspace_root,
        pane_id=request.pane_id,
        pane_pid=request.pane_pid,
        process_id=request.process_id,
        command=request.command,
        prompt_source=resolved_prompt.source,
        prompt_path=resolved_prompt.path,
        prompt_bytes=resolved_prompt.bytes,
        prompt_sha256=resolved_prompt.sha256,
        prompt_present=resolved_prompt.present,
        prompt_file_read=resolved_prompt.file_read,
        prompt_read_error=resolved_prompt.read_error,
        prompt_gate_ok=bool(prompt_gate_ok),
        prompt_gate_checked_forbidden_paths=list(prompt_gate_checked_forbidden_paths),
        authorization_record_path=request.authorization_record_path,
        live_enable_config_path=request.live_enable_config_path,
        allowed_write_roots=allowed_write_roots,
        forbidden_paths=forbidden_paths,
        expected_outputs=expected_outputs,
        report_path=report_path,
        terminal_markers=request.terminal_markers or list(DEFAULT_TERMINAL_MARKERS),
        created_at=created_at,
    )


def build_claude_tmux_submit_command_plan_from_request(request: ClaudeLiveSubmitRequest) -> ClaudeTmuxSubmitCommandPlan:
    config, _ = _load_config(request)
    authorization, auth_payload = _load_authorization(request.authorization_record_path)
    record = _record_from_payload(auth_payload) if auth_payload else None
    resolved_prompt = resolve_prompt_content(request)
    prompt_gate_ok, prompt_gate_checked_forbidden_paths = _prompt_gate_result(request, record, resolved_prompt)
    envelope = build_claude_live_delivery_envelope(request, record, resolved_prompt, prompt_gate_ok, prompt_gate_checked_forbidden_paths)
    plan = build_claude_live_submit_plan(request)
    decision = ClaudeLiveSubmitExecutionDecision(
        ok_to_execute=plan.live_submit_allowed,
        allow_real=request.allow_real,
        allow_tmux_input=request.allow_tmux_input,
        live_enabled=bool(config.live_enabled),
        adapter_available=True,
        authorization_ok=bool(authorization.get("ok", False)),
        authorization_not_expired="authorization expired" not in authorization.get("blocked_reasons", []),
        user_owned_pane_allowed=_user_owned_pane_allowed(request, record),
        workspace_allowed=_workspace_allowed(request, config),
        business_writes_forbidden=not _scope_blocks_business_write(request, record),
        task_dir_allowed=_task_dir_allowed(request, record),
        report_marker_protocol_required=request.require_report_marker_protocol,
        no_active_locks_required=config.require_no_active_locks,
        active_locks_clear=request.active_lock_count == 0,
        active_lock_count=request.active_lock_count,
        active_lock_evidence_source=request.active_lock_evidence_source,
        blocked_reasons=list(plan.blocked_reasons),
        warnings=list(plan.warnings),
    )
    return build_claude_tmux_submit_command_plan(request, envelope, decision, resolved_prompt)


def build_claude_tmux_submit_command_plan(
    request: ClaudeLiveSubmitRequest,
    envelope: ClaudeLiveDeliveryEnvelope,
    decision: ClaudeLiveSubmitExecutionDecision,
    resolved_prompt: ResolvedPromptContent | None = None,
) -> ClaudeTmuxSubmitCommandPlan:
    resolved_prompt = resolved_prompt or resolve_prompt_content(request)
    safe_task_id = _safe_token(request.task_id or request.worker_id or "claude-live-submit")
    buffer_name = f"omx-claude-{safe_task_id}"
    prompt_file = request.prompt_file or (str(PurePosixPath(request.task_dir) / "prompt.txt") if request.task_dir else "")
    evidence_paths = _dedupe([path for path in [prompt_file, envelope.report_path, request.authorization_record_path, request.live_enable_config_path] if path])
    command_sequence = [
        {"argv": ["tmux", "load-buffer", "-b", buffer_name, prompt_file], "purpose": "load prompt into controlled buffer"},
        {"argv": ["tmux", "paste-buffer", "-b", buffer_name, "-t", request.pane_id], "purpose": "paste controlled buffer into target pane"},
        {"argv": ["tmux", "send-keys", "-t", request.pane_id, "Enter"], "purpose": "submit pasted prompt"},
        {"argv": ["tmux", "delete-buffer", "-b", buffer_name], "purpose": "remove controlled buffer"},
    ]
    blocked = list(decision.blocked_reasons)
    if not prompt_file:
        blocked.append("prompt_file missing")
    if not request.pane_id:
        blocked.append("pane_id missing")
    blocked = _dedupe(blocked)
    return ClaudeTmuxSubmitCommandPlan(
        would_execute=decision.ok_to_execute and not blocked,
        command_sequence=command_sequence,
        uses_tmux_load_buffer=True,
        uses_tmux_paste_buffer=True,
        uses_tmux_send_enter=True,
        uses_tmux_delete_buffer=True,
        buffer_name=buffer_name,
        prompt_file=prompt_file,
        prompt_source=resolved_prompt.source,
        prompt_bytes=resolved_prompt.bytes,
        prompt_sha256=resolved_prompt.sha256,
        prompt_present=resolved_prompt.present,
        prompt_file_read=resolved_prompt.file_read,
        prompt_read_error=resolved_prompt.read_error,
        prompt_gate_ok=envelope.prompt_gate_ok,
        prompt_gate_checked_forbidden_paths=list(envelope.prompt_gate_checked_forbidden_paths),
        evidence_paths=evidence_paths,
        blocked_reasons=blocked,
        warnings=decision.warnings,
    )


def resolve_prompt_content(request: ClaudeLiveSubmitRequest) -> ResolvedPromptContent:
    if request.prompt_text:
        data = request.prompt_text.encode("utf-8")
        return ResolvedPromptContent(
            text=request.prompt_text,
            source="prompt_text",
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            present=True,
        )
    if not request.prompt_file:
        return ResolvedPromptContent(read_error="prompt missing")
    path = Path(request.prompt_file)
    try:
        stat = path.stat()
    except OSError as exc:
        return ResolvedPromptContent(source="prompt_file", path=str(path), read_error=f"prompt_file unreadable: {exc.strerror or exc.__class__.__name__}")
    if stat.st_size > MAX_PROMPT_FILE_BYTES:
        return ResolvedPromptContent(
            source="prompt_file",
            path=str(path),
            bytes=stat.st_size,
            read_error=f"prompt_file too large: {stat.st_size} > {MAX_PROMPT_FILE_BYTES}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ResolvedPromptContent(source="prompt_file", path=str(path), read_error=f"prompt_file unreadable: {exc.__class__.__name__}")
    data = text.encode("utf-8")
    return ResolvedPromptContent(
        text=text,
        source="prompt_file",
        path=str(path),
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        present=bool(text),
        file_read=True,
        read_error="" if text else "prompt missing",
    )


def execute_claude_tmux_submit(
    command_plan: ClaudeTmuxSubmitCommandPlan,
    *,
    allow_real: bool = False,
    allow_tmux_input: bool = False,
    command_runner: Callable[[Sequence[str]], Any] | None = None,
) -> ClaudeLiveSubmitResult:
    blocked = list(command_plan.blocked_reasons)
    if not allow_real:
        blocked.append("allow_real=false")
    if not allow_tmux_input:
        blocked.append("allow_tmux_input=false")
    if not command_plan.ok_to_execute:
        blocked.append("command plan ok_to_execute=false")
    blocked = _dedupe(blocked)
    if blocked:
        return ClaudeLiveSubmitResult(
            attempted=False,
            submitted=False,
            task_id=_task_id_from_prompt_file(command_plan.prompt_file),
            task_dir=str(PurePosixPath(command_plan.prompt_file).parent) if command_plan.prompt_file else "",
            executed=False,
            tmux_command_plan=command_plan.to_dict(),
            delivery_artifacts=list(command_plan.evidence_paths),
            blocked_reasons=blocked,
            warnings=_dedupe(command_plan.warnings + ["no live submit was attempted"]),
            evidence=[{"source": "claude_tmux_submit_command_plan", "detail": command_plan.to_dict()}],
        )
    _assert_safe_tmux_command_plan(command_plan)
    runner = command_runner or _run_command
    for item in command_plan.command_sequence:
        runner(item["argv"])
    return ClaudeLiveSubmitResult(
        attempted=True,
        submitted=True,
        task_id=_task_id_from_prompt_file(command_plan.prompt_file),
        task_dir=str(PurePosixPath(command_plan.prompt_file).parent) if command_plan.prompt_file else "",
        executed=True,
        tmux_command_plan=command_plan.to_dict(),
        delivery_artifacts=list(command_plan.evidence_paths),
        warnings=command_plan.warnings,
        evidence=[{"source": "claude_tmux_submit_command_plan", "detail": command_plan.to_dict()}],
    )


def _load_config(request: ClaudeLiveSubmitRequest) -> tuple[LiveEnableConfig, str]:
    path = Path(request.live_enable_config_path).resolve() if request.live_enable_config_path else None
    return load_live_enable_config(path, "claude_code")


def _load_authorization(path_text: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path_text:
        return (
            {
                "ok": False,
                "blocked_reasons": ["authorization record missing"],
                "warnings": ["authorization record is not live enable"],
            },
            None,
        )
    path = Path(path_text).resolve()
    validation = validate_authorization_record_file(path).to_dict()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        payload = None
    return validation, payload if isinstance(payload, dict) else None


def _record_from_payload(payload: dict[str, Any]) -> UserAuthorizationRecord:
    return UserAuthorizationRecord.from_dict(payload)


def _record_mismatch_blocks(request: ClaudeLiveSubmitRequest, record: UserAuthorizationRecord) -> list[str]:
    blocked: list[str] = []
    if record.backend_kind != "claude_code":
        blocked.append(f"authorization backend mismatch: {record.backend_kind}")
    if request.worker_id and record.worker_id != request.worker_id:
        blocked.append(f"authorization worker_id mismatch: {record.worker_id} != {request.worker_id}")
    if request.workspace_root and record.workspace_root != request.workspace_root:
        blocked.append(f"authorization workspace_root mismatch: {record.workspace_root} != {request.workspace_root}")
    if request.pane_id and record.pane_id and record.pane_id != request.pane_id:
        blocked.append(f"authorization pane_id mismatch: {record.pane_id} != {request.pane_id}")
    if request.process_id and record.process_id and record.process_id != request.process_id:
        blocked.append(f"authorization process_id mismatch: {record.process_id} != {request.process_id}")
    if not record.allow_live_submit:
        blocked.append("authorization allow_live_submit=false")
    if not record.allow_single_worker_smoke:
        blocked.append("authorization allow_single_worker_smoke=false")
    if record.allow_mixed_worker:
        blocked.append("authorization unexpectedly allows mixed worker")
    return blocked


def _user_owned_pane_allowed(request: ClaudeLiveSubmitRequest, record: UserAuthorizationRecord | None) -> bool:
    return (not request.user_owned_pane) or request.allow_user_owned_pane or bool(record and record.allow_user_owned_pane)


def _scope_blocks_business_write(
    request: ClaudeLiveSubmitRequest,
    record: UserAuthorizationRecord | None,
) -> bool:
    allow_business = bool(record and record.allow_business_repo_writes)
    roots = list(request.allowed_write_roots)
    if record is not None:
        roots.extend(record.allowed_write_roots)
    return any(_path_within(root, BUSINESS_REPO_PATH) for root in roots) and not allow_business


def _config_scope_blocks(request: ClaudeLiveSubmitRequest, config: LiveEnableConfig) -> list[str]:
    blocked: list[str] = []
    if request.workspace_root and config.allowed_workspace_roots and request.workspace_root not in config.allowed_workspace_roots:
        blocked.append(f"workspace root not allowed: {request.workspace_root}")
    if request.runtime_backend and config.allowed_runtime_substrates and request.runtime_backend not in config.allowed_runtime_substrates:
        blocked.append(f"runtime backend not allowed: {request.runtime_backend}")
    if request.worker_id and config.allowed_worker_ids and request.worker_id not in config.allowed_worker_ids:
        blocked.append(f"worker_id not allowed: {request.worker_id}")
    return blocked


def _workspace_allowed(request: ClaudeLiveSubmitRequest, config: LiveEnableConfig) -> bool:
    return not (
        request.workspace_root
        and config.allowed_workspace_roots
        and request.workspace_root not in config.allowed_workspace_roots
    )


def _task_dir_allowed(request: ClaudeLiveSubmitRequest, record: UserAuthorizationRecord | None) -> bool:
    if not request.task_dir:
        return True
    if SMOKE_ARTIFACT_ROOT_FRAGMENT in request.task_dir:
        return True
    roots = list(request.allowed_write_roots)
    if record is not None:
        roots.extend(record.allowed_write_roots)
    return any(_path_within(request.task_dir, root) for root in roots)


def _business_forbidden_without_prompt_guard(
    request: ClaudeLiveSubmitRequest,
    record: UserAuthorizationRecord | None,
) -> bool:
    resolved_prompt = resolve_prompt_content(request)
    prompt_gate_ok, _ = _prompt_gate_result(request, record, resolved_prompt)
    return not prompt_gate_ok


def _prompt_gate_result(
    request: ClaudeLiveSubmitRequest,
    record: UserAuthorizationRecord | None,
    resolved_prompt: ResolvedPromptContent,
) -> tuple[bool, list[str]]:
    forbidden = list(request.forbidden_paths)
    if record is not None:
        forbidden.extend(record.forbidden_paths)
    checked = _dedupe(
        [path for path in forbidden if _path_within(BUSINESS_REPO_PATH, path) or _path_within(path, BUSINESS_REPO_PATH)]
    )
    if not checked:
        return True, []
    if not resolved_prompt.present:
        return False, checked
    return _prompt_forbids_business_writes(resolved_prompt.text), checked


def _prompt_forbids_business_writes(prompt_text: str) -> bool:
    prompt = prompt_text.lower()
    mentions_business = "business repo" in prompt or BUSINESS_REPO_PATH.lower() in prompt
    forbids_write = any(
        phrase in prompt
        for phrase in (
            "do not write",
            "do not modify",
            "no business repo writes",
            "no business repo write",
            "禁止",
        )
    )
    return mentions_business and forbids_write


def _safe_token(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return cleaned.strip("-")[:80] or "claude-live-submit"


def _task_id_from_prompt_file(prompt_file: str) -> str:
    if not prompt_file:
        return ""
    parent = PurePosixPath(prompt_file).parent
    return parent.name if parent.name else ""


def _assert_safe_tmux_command_plan(command_plan: ClaudeTmuxSubmitCommandPlan) -> None:
    expected = [
        ("load-buffer",),
        ("paste-buffer",),
        ("send-keys", "Enter"),
        ("delete-buffer",),
    ]
    if len(command_plan.command_sequence) != 4:
        raise RuntimeAdapterError("Claude tmux command plan must contain exactly four audited commands")
    for item, expected_tokens in zip(command_plan.command_sequence, expected):
        argv = item.get("argv", [])
        if not isinstance(argv, list) or not argv or argv[0] != "tmux":
            raise RuntimeAdapterError("Claude tmux command plan may only execute tmux argv lists")
        joined = " ".join(str(part) for part in argv)
        if any(token in joined for token in ("C-c", "Escape", "kill", "kill-pane", "kill-window", "respawn", "split-window")):
            raise RuntimeAdapterError(f"unsafe tmux command in Claude submit plan: {joined}")
        for token in expected_tokens:
            if token not in argv:
                raise RuntimeAdapterError(f"Claude tmux command missing required token {token}: {joined}")


def _run_command(argv: Sequence[str]) -> None:
    subprocess.run(list(argv), check=True)


def _path_within(path: str, root: str) -> bool:
    if not path or not root:
        return False
    candidate = PurePosixPath(path)
    base = PurePosixPath(root)
    if not candidate.is_absolute() or not base.is_absolute():
        return False
    return candidate == base or base in candidate.parents


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


__all__ = [
    "ClaudeLiveDeliveryEnvelope",
    "ClaudeLiveSubmitExecutionDecision",
    "ClaudeLiveSubmitPlan",
    "ClaudeLiveSubmitRequest",
    "ClaudeLiveSubmitResult",
    "ClaudeTmuxSubmitCommandPlan",
    "build_claude_live_submit_plan",
    "build_claude_live_submit_result",
    "build_claude_tmux_submit_command_plan",
    "build_claude_tmux_submit_command_plan_from_request",
    "execute_claude_tmux_submit",
    "resolve_prompt_content",
]
