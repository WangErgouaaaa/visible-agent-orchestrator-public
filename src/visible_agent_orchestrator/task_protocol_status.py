"""Shared task protocol marker/status reader.

Single source of truth for marker/report based task status classification.
Consumers: bin/omx_task_signal (authoritative snapshots), bin/omx_team_dispatch
(run status), bin/omx_subagent_sidebar (display projection input).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.terminal_acceptance import (
    terminal_acceptance_status,
)

TERMINAL_MARKERS = ("done", "fail", "blocked")
FLOW_MARKERS = ("submitted", "received", "started", "heartbeat")
STATE_TO_MARKER = {"done": "done", "fail": "fail", "blocked": "blocked"}
STATE_TO_STATUS = {"done": "completed", "fail": "failed", "blocked": "blocked"}


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def marker_path(tdir: Path, name: str) -> Path:
    return tdir / f".{name}"


def marker_exists(tdir: Path, name: str) -> bool:
    return marker_path(tdir, name).is_file()


def task_protocol_status(tdir: Path) -> dict[str, Any]:
    """Classify one task directory from its markers and report.

    Terminal truth requires exactly one terminal marker plus a non-empty
    report.md; anything else is surfaced as an invalid or in-flight state,
    never silently promoted to a terminal status.
    """
    acceptance = terminal_acceptance_status(tdir)
    terminal = [name for name in TERMINAL_MARKERS if marker_exists(tdir, name)]
    flow = [name for name in FLOW_MARKERS if marker_exists(tdir, name)]
    report = tdir / "report.md"
    report_bytes = file_size(report)
    status = "prepared"
    reason = "task directory exists"
    if len(terminal) > 1:
        status = "invalid_conflicting_terminal_markers"
        reason = "multiple terminal markers exist"
    elif terminal:
        term = terminal[0]
        if report_bytes <= 0:
            status = "invalid_terminal_no_report"
            reason = f".{term} exists without non-empty report.md"
        elif acceptance.get("required") and not acceptance.get("accepted"):
            status = "invalid_terminal_acceptance"
            reason = (
                "terminal markers present but acceptance fence refused: "
                + str(acceptance.get("reason") or "")
            )
        else:
            status = STATE_TO_STATUS.get(term, term)
            reason = f"report.md plus .{term}"
    elif "submitted" in flow and "received" not in flow:
        status = "delivery_uncertain"
        reason = ".submitted exists without .received"
    elif "received" in flow and "started" not in flow:
        status = "received"
        reason = ".received exists without .started"
    elif "started" in flow or "heartbeat" in flow:
        status = "running"
        reason = ".started or .heartbeat exists"
    return {
        "status": status,
        "reason": reason,
        "flow_markers": flow,
        "terminal_markers": terminal,
        "report_exists": report.is_file(),
        "report_bytes": report_bytes,
        "report_nonempty": report_bytes > 0,
        "terminal_acceptance": acceptance,
    }
