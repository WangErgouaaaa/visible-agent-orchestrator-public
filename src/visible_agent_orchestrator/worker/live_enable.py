"""Fail-closed live enable gate for worker agent backends."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError

from .backends import available_backends


PUBLIC_BACKEND_BY_CANONICAL = {
    "opencode-tui": "opencode_tui",
    "claude-code-fixture": "claude_code",
    "cursor-agent-fixture": "cursor_agent",
}

CANONICAL_BACKEND_BY_PUBLIC = {value: key for key, value in PUBLIC_BACKEND_BY_CANONICAL.items()}
CANONICAL_BACKEND_BY_PUBLIC.update({key: key for key in PUBLIC_BACKEND_BY_CANONICAL})


def canonical_backend_kind(backend_kind: str) -> str:
    try:
        return CANONICAL_BACKEND_BY_PUBLIC[backend_kind]
    except KeyError as exc:
        raise RuntimeAdapterError(f"worker backend is not available: {backend_kind}") from exc


def public_backend_kind(backend_kind: str) -> str:
    canonical = canonical_backend_kind(backend_kind)
    return PUBLIC_BACKEND_BY_CANONICAL[canonical]


def backend_metadata(backend_kind: str) -> dict[str, Any]:
    canonical = canonical_backend_kind(backend_kind)
    for item in available_backends():
        if item["name"] == canonical:
            return item
    raise RuntimeAdapterError(f"worker backend is not available: {backend_kind}")


def backend_supports_live_submit(backend_kind: str) -> bool:
    return canonical_backend_kind(backend_kind) in {"opencode-tui", "claude-code-fixture"}


def backend_live_submit_adapter_available(backend_kind: str) -> bool:
    public = public_backend_kind(backend_kind)
    return public in {"opencode_tui", "claude_code"}


@dataclass(frozen=True)
class LiveEnableConfig:
    backend_kind: str
    live_enabled: bool = False
    supports_live_submit_required: bool = True
    enabled_by: str = ""
    enabled_at: str = ""
    user_authorization_record: str = ""
    allowed_workspace_roots: list[str] = field(default_factory=list)
    allowed_runtime_substrates: list[str] = field(default_factory=list)
    allowed_worker_ids: list[str] = field(default_factory=list)
    allow_user_owned_pane: bool = False
    require_preflight_ok: bool = True
    require_no_active_locks: bool = True
    require_business_index_unchanged: bool = True
    require_single_worker_only: bool = True
    allow_mixed_worker: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def disabled_default(cls, backend_kind: str) -> "LiveEnableConfig":
        public = public_backend_kind(backend_kind)
        if public == "opencode_tui":
            return cls(
                backend_kind=public,
                live_enabled=True,
                user_authorization_record="existing-visible-dispatch-wrapper",
                allowed_runtime_substrates=["tmux-reference"],
                allow_user_owned_pane=False,
                notes=["OpenCode live submit remains wrapper-only through visible dispatch."],
            )
        return cls(
            backend_kind=public,
            live_enabled=False,
            allowed_runtime_substrates=["tmux-reference"],
            notes=[f"{public} is disabled by default and fixture-only."],
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any], backend_kind: str) -> "LiveEnableConfig":
        public = public_backend_kind(backend_kind)
        source = payload
        backends = payload.get("backends")
        if isinstance(backends, dict):
            selected = backends.get(public) or backends.get(canonical_backend_kind(public))
            if selected is None:
                return cls.disabled_default(public)
            if not isinstance(selected, dict):
                raise RuntimeAdapterError(f"live enable config for {public} must be an object")
            source = selected
        elif payload.get("backend_kind") and public_backend_kind(str(payload["backend_kind"])) != public:
            return cls.disabled_default(public)

        data = {
            "backend_kind": public,
            "live_enabled": bool(source.get("live_enabled", False)),
            "supports_live_submit_required": bool(source.get("supports_live_submit_required", True)),
            "enabled_by": str(source.get("enabled_by", "")),
            "enabled_at": str(source.get("enabled_at", "")),
            "user_authorization_record": str(source.get("user_authorization_record", "")),
            "allowed_workspace_roots": _string_list(source.get("allowed_workspace_roots", [])),
            "allowed_runtime_substrates": _string_list(source.get("allowed_runtime_substrates", [])),
            "allowed_worker_ids": _string_list(source.get("allowed_worker_ids", [])),
            "allow_user_owned_pane": bool(source.get("allow_user_owned_pane", False)),
            "require_preflight_ok": bool(source.get("require_preflight_ok", True)),
            "require_no_active_locks": bool(source.get("require_no_active_locks", True)),
            "require_business_index_unchanged": bool(source.get("require_business_index_unchanged", True)),
            "require_single_worker_only": bool(source.get("require_single_worker_only", True)),
            "allow_mixed_worker": bool(source.get("allow_mixed_worker", False)),
            "notes": _string_list(source.get("notes", [])),
        }
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LiveEnableDecision:
    eligible: bool
    backend_kind: str
    live_enabled: bool
    capability_supports_live_submit: bool
    live_submit_adapter_available: bool
    config_allows_live: bool
    user_authorized: bool
    workspace_allowed: bool
    runtime_allowed: bool
    worker_id_allowed: bool
    user_owned_pane_allowed: bool
    preflight_required: bool
    no_active_locks_required: bool
    business_index_unchanged_required: bool
    single_worker_only: bool
    mixed_worker_allowed: bool
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def load_live_enable_config(path: Path | None, backend_kind: str) -> tuple[LiveEnableConfig, str]:
    if path is None:
        return LiveEnableConfig.disabled_default(backend_kind), "default-disabled"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeAdapterError(f"live enable config missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeAdapterError(f"invalid live enable config: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeAdapterError(f"live enable config must contain an object: {path}")
    return LiveEnableConfig.from_dict(payload, backend_kind), str(path)


def evaluate_live_enable(
    *,
    backend_kind: str,
    config: LiveEnableConfig | None = None,
    workspace_root: str = "",
    worker_id: str = "",
    runtime_backend: str = "",
    user_owned_pane: bool = False,
    config_source: str = "default-disabled",
) -> LiveEnableDecision:
    public = public_backend_kind(backend_kind)
    metadata = backend_metadata(public)
    cfg = config or LiveEnableConfig.disabled_default(public)
    capability = backend_supports_live_submit(public)
    adapter_available = backend_live_submit_adapter_available(public)
    config_allows_live = bool(cfg.live_enabled)
    user_authorized = bool(cfg.user_authorization_record)
    workspace_allowed = _allowed_or_unspecified(workspace_root, cfg.allowed_workspace_roots)
    runtime_allowed = _allowed_or_unspecified(runtime_backend, cfg.allowed_runtime_substrates)
    worker_id_allowed = _allowed_or_unspecified(worker_id, cfg.allowed_worker_ids)
    user_owned_pane_allowed = (not user_owned_pane) or bool(cfg.allow_user_owned_pane)

    blocked: list[str] = []
    warnings: list[str] = []
    if not capability:
        blocked.append(f"{public} capability supports_live_submit=false")
    if cfg.supports_live_submit_required and not capability:
        blocked.append(f"{public} requires live submit support but capability is false")
    if not config_allows_live:
        blocked.append(f"{public} live_enabled=false")
    if public in {"claude_code", "cursor_agent"} and not user_authorized:
        blocked.append(f"{public} missing user_authorization_record")
    if public == "claude_code" and config_allows_live:
        if not workspace_root:
            blocked.append("claude_code live enable requires workspace_root")
        if not worker_id:
            blocked.append("claude_code live enable requires worker_id")
        if not runtime_backend:
            blocked.append("claude_code live enable requires runtime_backend")
    if workspace_root and not workspace_allowed:
        blocked.append(f"workspace root not allowed: {workspace_root}")
    if runtime_backend and not runtime_allowed:
        blocked.append(f"runtime backend not allowed: {runtime_backend}")
    if worker_id and not worker_id_allowed:
        blocked.append(f"worker_id not allowed: {worker_id}")
    if user_owned_pane and not user_owned_pane_allowed:
        blocked.append("user-owned pane not allowed without explicit authorization and separate registration flow")
    if public == "opencode_tui":
        warnings.append("opencode_tui live submit is wrapper-only through omx_visible_dispatch; direct submit is forbidden")
    if public in {"claude_code", "cursor_agent"}:
        warnings.append(f"{public} remains disabled by default; no live worker is contacted")
    if public == "claude_code" and adapter_available:
        warnings.append("claude_code technical visible-pane live adapter exists but is disabled by live-enable gates")

    eligible = not blocked
    return LiveEnableDecision(
        eligible=eligible,
        backend_kind=public,
        live_enabled=eligible and capability and config_allows_live,
        capability_supports_live_submit=capability,
        live_submit_adapter_available=adapter_available,
        config_allows_live=config_allows_live,
        user_authorized=user_authorized,
        workspace_allowed=workspace_allowed,
        runtime_allowed=runtime_allowed,
        worker_id_allowed=worker_id_allowed,
        user_owned_pane_allowed=user_owned_pane_allowed,
        preflight_required=cfg.require_preflight_ok,
        no_active_locks_required=cfg.require_no_active_locks,
        business_index_unchanged_required=cfg.require_business_index_unchanged,
        single_worker_only=cfg.require_single_worker_only,
        mixed_worker_allowed=cfg.allow_mixed_worker,
        blocked_reasons=blocked,
        warnings=warnings,
        evidence=[
            {
                "source": "live_enable_config",
                "detail": {"config_source": config_source, "backend_kind": cfg.backend_kind},
            },
            {
                "source": "backend_capability",
                "detail": {
                    "canonical_backend": metadata["name"],
                    "supports_live_submit": capability,
                    "live_submit_adapter_available": adapter_available,
                    "direct_submit_allowed": False,
                },
            },
        ],
    )


def _allowed_or_unspecified(value: str, allowed: list[str]) -> bool:
    if not value or not allowed:
        return True
    return value in allowed


__all__ = [
    "LiveEnableConfig",
    "LiveEnableDecision",
    "backend_live_submit_adapter_available",
    "backend_supports_live_submit",
    "evaluate_live_enable",
    "load_live_enable_config",
    "public_backend_kind",
]
