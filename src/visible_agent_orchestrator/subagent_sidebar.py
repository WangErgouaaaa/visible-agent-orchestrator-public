from __future__ import annotations

from typing import Any

from visible_agent_orchestrator.subagent_display import (
    DEFAULT_WIDTH,
    MIN_WIDTH,
    CompactProjectionOptions,
    render_compact_projection,
)


def render_sidebar(projection: dict[str, Any], *, width: int = DEFAULT_WIDTH) -> str:
    return render_compact_projection(
        projection,
        CompactProjectionOptions(
            title="OpenCode Subagent Sidebar",
            width=max(MIN_WIDTH, int(width or DEFAULT_WIDTH)),
            label_subagent_fields=False,
            include_metadata=False,
            row_layout=True,
            warning_prefix="! ",
            empty_ledger_present_message="(no subagent ledger yet)",
        ),
    )
