from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visible_agent_orchestrator.runtime.health import classify_runtime_telemetry


def test_runtime_telemetry_keeps_post_terminal_continuation_nonfatal() -> None:
    telemetry = {
        "tui_readiness": "busy",
        "post_terminal_tui_busy": True,
        "post_terminal_continuation_state": "post_terminal_continuation_stalled",
        "active_alerts": 0,
        "alerts": [],
        "locks": [],
    }
    readiness = type(
        "Readiness",
        (),
        {"readiness": "busy", "reason": "continuation still active"},
    )()

    summary = classify_runtime_telemetry(readiness, telemetry)

    assert summary["ok"] is True
    assert summary["state"] == "TELEMETRY"
    assert summary["post_terminal_continuation_state"] == (
        "post_terminal_continuation_stalled"
    )
    assert summary["post_terminal_tui_busy"] is True
