"""Worker agent backend registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError

from .adapter import WorkerBackend
from .fixture import ClaudeCodeFixtureBackend, CursorAgentFixtureBackend


def available_backends() -> list[dict[str, Any]]:
    return [
        {
            "name": "opencode-tui",
            "agent_kind": "opencode",
            "status": "current/live-via-existing-visible-dispatch",
            "default": True,
            "live_enabled": True,
            "live_submit_adapter_available": True,
            "live_submit_runtime_enabled": True,
            "fixture_only": False,
            "runtime_backend": "tmux-reference",
            "dispatch_adapter": "opencode_tui",
            "notes": "Existing OpenCode live path remains omx_visible_dispatch; this CLI does not replace it.",
        },
        {
            "name": "codex-tui",
            "agent_kind": "codex",
            "status": "current/live-via-codex-appserver-adapter",
            "default": False,
            "live_enabled": True,
            "live_submit_adapter_available": True,
            "live_submit_runtime_enabled": True,
            "fixture_only": False,
            "runtime_backend": "tmux-reference",
            "dispatch_adapter": "codex_tui",
            "notes": "Codex CLI TUI workers controlled via the app-server WebSocket unix socket (bin/omx_codex_tui_adapter + bin/omx_codex_appserver_client); no OpenCode port or ses_ session.",
        },
        {
            "name": "claude-code-fixture",
            "agent_kind": "claude-code",
            "status": "candidate/fixture-only",
            "default": False,
            "live_enabled": False,
            "live_submit_adapter_available": True,
            "live_submit_runtime_enabled": False,
            "fixture_only": True,
            "runtime_backend": "unassigned",
            "dispatch_adapter": "claude_visible_pane_plan",
            "notes": "Claude Code visible-pane live adapter model exists, but live enable and registration remain disabled.",
        },
        {
            "name": "cursor-agent-fixture",
            "agent_kind": "cursor-agent",
            "status": "candidate/fixture-only",
            "default": False,
            "live_enabled": False,
            "live_submit_adapter_available": False,
            "live_submit_runtime_enabled": False,
            "fixture_only": True,
            "runtime_backend": "unassigned",
            "dispatch_adapter": "none",
            "notes": "No Cursor Agent process is contacted or registered.",
        },
    ]


def default_backend_name() -> str:
    return "opencode-tui"


def get_backend(name: str, fixture_dir: Path | None = None) -> WorkerBackend:
    if name == "claude-code-fixture":
        return ClaudeCodeFixtureBackend(fixture_dir=fixture_dir)
    if name == "cursor-agent-fixture":
        return CursorAgentFixtureBackend(fixture_dir=fixture_dir)
    if name == "opencode-tui":
        raise RuntimeAdapterError(
            "opencode-tui worker backend is the existing live path; use omx_visible_dispatch and RuntimeAdapter evidence"
        )
    raise RuntimeAdapterError(f"worker backend is not available: {name}")


def is_backend_live_enabled(name: str) -> bool:
    for backend in available_backends():
        if backend["name"] == name:
            return bool(backend["live_enabled"])
    raise RuntimeAdapterError(f"worker backend is not available: {name}")
