"""Dry-run Obsidian logging opt-in policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ObsidianLoggingPolicyResult:
    ok: bool
    dry_run_only: bool
    would_write: bool
    allow_obsidian_log: bool
    target_note: str = ""
    target_vault_path: str = ""
    purpose: str = ""
    allowed_content_scope: str = ""
    append_only: bool = False
    rollback_allowed: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def validate_obsidian_logging_request(
    *,
    allow_obsidian_log: bool = False,
    target_note: str = "",
    target_vault_path: str = "",
    purpose: str = "",
    allowed_content_scope: str = "",
    append_only: bool = False,
    rollback_allowed: bool = False,
) -> ObsidianLoggingPolicyResult:
    blocked: list[str] = []
    warnings = [
        "Obsidian logging is an external side effect",
        "Obsidian notes are not runtime truth",
        "policy check is dry-run only and does not write notes",
    ]
    if not allow_obsidian_log:
        blocked.append("explicit Obsidian logging opt-in is required")
    if allow_obsidian_log and not target_note:
        blocked.append("target note path is required")
    if allow_obsidian_log and not purpose.strip():
        blocked.append("purpose is required")
    if allow_obsidian_log and not append_only:
        blocked.append("append-only authorization is required")
    if target_note and allow_obsidian_log and not Path(target_note).is_file():
        blocked.append(f"target note does not exist: {target_note}")

    return ObsidianLoggingPolicyResult(
        ok=not blocked,
        dry_run_only=True,
        would_write=False,
        allow_obsidian_log=allow_obsidian_log,
        target_note=target_note,
        target_vault_path=target_vault_path,
        purpose=purpose,
        allowed_content_scope=allowed_content_scope,
        append_only=append_only,
        rollback_allowed=rollback_allowed,
        blocked_reasons=_dedupe(blocked),
        warnings=_dedupe(warnings),
        safe_next_actions=[
            "record exact vault and note path in final report",
            "write only after a separate explicit user request",
        ],
        forbidden_next_actions=[
            "write Obsidian during source-only tasks by default",
            "treat Obsidian notes as runtime truth",
            "auto-rollback Obsidian without explicit user authorization",
        ],
    )


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


__all__ = ["ObsidianLoggingPolicyResult", "validate_obsidian_logging_request"]
