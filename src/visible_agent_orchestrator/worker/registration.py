"""Dry-run registration planning for candidate live worker backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .live_enable import LiveEnableConfig, evaluate_live_enable, public_backend_kind


@dataclass(frozen=True)
class DryRunRegistrationRequest:
    backend_kind: str
    worker_id: str
    workspace_root: str
    runtime_backend: str
    runtime_binding_kind: str = ""
    pane_id: str = ""
    process_id: str = ""
    port: str = ""
    user_owned_pane: bool = False
    user_authorization_record: str = ""
    allowed_write_roots: list[str] = field(default_factory=list)
    allowed_read_roots: list[str] = field(default_factory=list)
    task_profile: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DryRunRegistrationPlan:
    would_register: bool
    would_write_files: bool
    would_modify_live_registry: bool
    backend_kind: str
    worker_id: str
    workspace_root: str
    runtime_backend: str
    registration_allowed: bool
    live_submit_allowed: bool
    required_preflight_checks: list[str] = field(default_factory=list)
    required_user_confirmations: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def build_dry_run_registration_plan(
    request: DryRunRegistrationRequest,
    *,
    config: LiveEnableConfig | None = None,
    config_source: str = "default-disabled",
) -> DryRunRegistrationPlan:
    public = public_backend_kind(request.backend_kind)
    effective_config = config
    if request.user_authorization_record and config is not None:
        effective_config = LiveEnableConfig(
            **{
                **config.to_dict(),
                "user_authorization_record": request.user_authorization_record,
            }
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
    blocked = list(decision.blocked_reasons)
    if public in {"claude_code", "cursor_agent"}:
        blocked.append(f"{public} registration is disabled until a separate live enable gate commit")
    if request.user_owned_pane:
        blocked.append("user-owned pane requires explicit user authorization and independent registration review")

    registration_allowed = False
    live_submit_allowed = False
    return DryRunRegistrationPlan(
        would_register=False,
        would_write_files=False,
        would_modify_live_registry=False,
        backend_kind=public,
        worker_id=request.worker_id,
        workspace_root=request.workspace_root,
        runtime_backend=request.runtime_backend,
        registration_allowed=registration_allowed,
        live_submit_allowed=live_submit_allowed,
        required_preflight_checks=[
            "source tests pass",
            "fixture plan/validate pass",
            "no active locks",
            "watcher alerts=0",
            "board validate no errors",
            "business repo index unchanged",
            "exact worker/pane/process/workspace identified",
        ],
        required_user_confirmations=[
            "which worker",
            "which pane/process",
            "which workspace",
            "task type",
            "allowed write scope",
            "whether business repo writes are allowed",
            "rollback/recovery plan",
        ],
        required_artifacts=[
            "report.md",
            "exactly one of .done/.fail/.blocked",
            ".received",
            ".started",
            "dry-run registration review",
            "single-worker no-op smoke record",
        ],
        blocked_reasons=blocked,
        warnings=[
            "dry-run registration does not write live registry",
            "dry-run registration does not write the selected runtime root",
            "dry-run registration does not call live worker commands",
        ],
        forbidden_actions=[
            "write live registry",
            "write the selected runtime root",
            "touch user-owned Claude Code panels",
            "tmux send-keys",
            "tmux paste-buffer",
            "claude/claude-code command",
            "cursor-agent command",
            "OpenCode live submit",
            "rmux live",
            "direct adapter submit",
            "direct /tui/*",
        ],
        evidence=[
            {"source": "dry_run_request", "detail": request.to_dict()},
            {"source": "live_enable_decision", "detail": decision.to_dict()},
        ],
    )


__all__ = [
    "DryRunRegistrationPlan",
    "DryRunRegistrationRequest",
    "build_dry_run_registration_plan",
]
