"""Read-only forensics for post-terminal OMO continuation behavior."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_TS_RE = re.compile(r"(?:^\s*|\[)(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\b")
_INCOMPLETE_RE = re.compile(r"\bincompleteCount=(\d+)\b")
_MODEL_RE = re.compile(r"\bmodel=([^\s]+)")
_JSON_PAYLOAD_RE = re.compile(r"(\{.*\})\s*$")


def _timestamp_from_line(line: str) -> str:
    match = _TS_RE.search(line)
    return match.group(1) if match else ""


def _json_payload_from_line(line: str) -> dict[str, Any]:
    match = _JSON_PAYLOAD_RE.search(line)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def analyze_omo_continuation_log(text: str, *, session_id: str) -> dict[str, Any]:
    session = str(session_id or "").strip()
    lines = [line for line in str(text or "").splitlines() if session and session in line]
    message_aborted_seen = False
    idle_with_incomplete_seen = False
    continuation_injected_seen = False
    all_todos_complete_seen = False
    incomplete_count: int | None = None
    model = ""
    message_aborted_at = ""
    idle_with_incomplete_at = ""
    continuation_injected_at = ""
    all_todos_complete_at = ""

    for raw_line in lines:
        line = raw_line.strip()
        timestamp = _timestamp_from_line(line)
        payload = _json_payload_from_line(line)
        if "MessageAbortedError" in line:
            message_aborted_seen = True
            if not message_aborted_at:
                message_aborted_at = timestamp
        if "session.idle" in line:
            match = _INCOMPLETE_RE.search(line)
            if match:
                idle_with_incomplete_seen = True
                incomplete_count = int(match.group(1))
                if not idle_with_incomplete_at:
                    idle_with_incomplete_at = timestamp
        if "Countdown started" in line:
            payload_incomplete = payload.get("incompleteCount")
            if isinstance(payload_incomplete, int):
                idle_with_incomplete_seen = True
                incomplete_count = payload_incomplete
                if not idle_with_incomplete_at:
                    idle_with_incomplete_at = timestamp
        if "Injecting continuation" in line:
            continuation_injected_seen = True
            if not continuation_injected_at:
                continuation_injected_at = timestamp
            model_match = _MODEL_RE.search(line)
            if model_match and not model:
                model = model_match.group(1)
            payload_incomplete = payload.get("incompleteCount")
            if isinstance(payload_incomplete, int) and incomplete_count is None:
                incomplete_count = payload_incomplete
            payload_model = payload.get("model")
            if isinstance(payload_model, dict):
                payload_model_id = payload_model.get("modelID")
                if isinstance(payload_model_id, str) and payload_model_id and not model:
                    model = payload_model_id
        if "All todos complete" in line:
            all_todos_complete_seen = True
            if not all_todos_complete_at:
                all_todos_complete_at = timestamp

    source = ""
    post_terminal_continuation_state = ""
    if continuation_injected_seen:
        source = "omo_todo_continuation_enforcer"
        if all_todos_complete_seen:
            post_terminal_continuation_state = "post_terminal_continuation_cleared"
        elif idle_with_incomplete_seen:
            post_terminal_continuation_state = "post_terminal_continuation_stalled"
        else:
            post_terminal_continuation_state = "post_terminal_continuation_running"

    return {
        "session_id": session,
        "source": source,
        "post_terminal_continuation_state": post_terminal_continuation_state,
        "line_count": len(lines),
        "message_aborted_seen": message_aborted_seen,
        "idle_with_incomplete_seen": idle_with_incomplete_seen,
        "continuation_injected_seen": continuation_injected_seen,
        "all_todos_complete_seen": all_todos_complete_seen,
        "incomplete_count": incomplete_count,
        "model": model,
        "message_aborted_at": message_aborted_at,
        "idle_with_incomplete_at": idle_with_incomplete_at,
        "continuation_injected_at": continuation_injected_at,
        "all_todos_complete_at": all_todos_complete_at,
    }


def analyze_omo_continuation_file(log_path: Path, *, session_id: str) -> dict[str, Any]:
    text = Path(log_path).read_text(encoding="utf-8")
    payload = analyze_omo_continuation_log(text, session_id=session_id)
    payload["omo_log"] = str(Path(log_path).resolve())
    return payload
