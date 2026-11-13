#!/usr/bin/env python3
"""Deterministic visible-task protocol truth helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_FILE = Path(__file__).resolve()
for _candidate in (
    Path(os.environ["OMX_VISIBLE_AGENT_ORCHESTRATOR_LIB"]).expanduser().resolve()
    if os.environ.get("OMX_VISIBLE_AGENT_ORCHESTRATOR_LIB")
    else None,
    (_THIS_FILE.parent.parent / "src").resolve(),
    (_THIS_FILE.parent.parent / "lib").resolve(),
):
    if _candidate is not None and _candidate.exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from visible_agent_orchestrator.progress_checkpoints import (
    classify_notable,
    earliest_page,
    identity_valid_records,
    latest_seq,
    read_checkpoint_access,
    read_progress_checkpoints,
    store_latest_seq,
)

from visible_agent_orchestrator.task_usage import (
    TaskUsageError,
    build_usage_payload,
)

from visible_agent_orchestrator.terminal_acceptance import (
    FLOW_MARKERS,
    TERMINAL_REPORT_SETTLE_SECONDS,
    terminal_acceptance_status,
)
from visible_agent_orchestrator.terminal_durability import (
    TERMINAL_RECOVERY_ACTION,
    durable_atomic_write,
    fsync_directory,
    pin_terminal_authority,
)

from visible_agent_orchestrator.visible_registry_lock import (
    owner_lock_transition_fence,
    visible_registry_lock,
    write_json_atomic,
)
from visible_agent_orchestrator.runtime_paths import runtime_root


TERMINAL_MARKERS = ("done", "fail", "blocked")
VALID_TERMINAL_STATES = {"completed", "failed", "blocked"}
CONTEXT_OVERFLOW_STATUS = "blocked_context_overflow"
CONTEXT_COMPACTION_STATUS = "blocked_context_compaction_review"
VERBAL_TERMINAL_STATUS = "verbal_terminal_without_protocol_signal"
VERBAL_TERMINAL_NEXT_SAFE_ACTION = "manual_operator_review_or_recovery_handshake_no_blind_resend"
CONTEXT_OVERFLOW_PHRASES = (
    "your input exceeds the context window",
    "context window of this model",
    "context_length_exceeded",
)
CONTEXT_COMPACTION_PHRASES = (
    "session.compacted",
    "session compacted",
    "context.compacted",
    "context compaction completed",
    "manual /compact",
    "manual /compat",
)
CONTEXT_OVERFLOW_NEXT_SAFE_ACTION = "manual_operator_review_no_blind_resend"
CONTEXT_COMPACTION_NEXT_SAFE_ACTION = "manual_operator_verify_terminal_truth_no_blind_resend"
PAYLOAD_EXCEEDED_NEXT_SAFE_ACTION = (
    "bounded_inspect_status_locks_session_preflight_no_full_session_messages"
)
PROVIDER_QUOTA_NEXT_SAFE_ACTION = (
    "replenish_quota_then_observe_same_owner_no_auto_resend"
)
CONTEXT_COMPACTION_OBSERVATION_FILES = ("context_compaction_observed.json",)
TERMINAL_SENTINEL_OBSERVATION_FILES = ("terminal_sentinel_observation.json",)
REVIEWER_PROGRESS_SIDECAR_GLOBS = (
    "subagent_ledger.jsonl",
    "reviewer-call-records.jsonl",
    "prompt-*.txt",
    "launcher-response-*.txt",
    "raw-verdict-*.json",
)


class TaskStateError(Exception):
    """Expected operator-facing failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def root_path(value: str | None) -> Path:
    return runtime_root(value)


def framework_root(root: Path) -> Path:
    return root / ".omx" / "framework" / "team"


def visible_runs_root(root: Path) -> Path:
    return framework_root(root) / "visible-runs"


def finalizer_fixture_runs_root(root: Path, fixture: str) -> Path:
    safe_id(fixture, "fixture")
    return framework_root(root) / "finalizer-fixtures" / fixture / "visible-runs"


def multiworker_fixture_runs_root(root: Path, fixture: str) -> Path:
    safe_id(fixture, "fixture")
    return framework_root(root) / "multiworker-fixtures" / fixture / "visible-runs"


def fixture_runs_root(root: Path, fixture: str) -> Path:
    safe_id(fixture, "fixture")
    multiworker = multiworker_fixture_runs_root(root, fixture)
    if multiworker.is_dir():
        return multiworker
    return finalizer_fixture_runs_root(root, fixture)


def locks_root_for_runs(runs_root: Path) -> Path:
    return runs_root / "_locks"


def lock_archive_root_for_runs(runs_root: Path) -> Path:
    return locks_root_for_runs(runs_root) / "archive"


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


VISIBLE_RUNS_PREFIX = "visible-runs/"
RESULT_RECEIPT_SCHEMA = "visible-task-result-receipt/v1"


def safe_id(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise TaskStateError(f"{label} must be a simple id, got: {value!r}")
    return value


def canonical_run_id(value: str, *, label: str = "run") -> str:
    raw = str(value or "").strip()
    if raw.startswith(VISIBLE_RUNS_PREFIX):
        simple = raw[len(VISIBLE_RUNS_PREFIX):].strip("/")
        hint = simple if simple and "/" not in simple and "\\" not in simple else "<run_id>"
        raise TaskStateError(
            f"{label} is the simple run_id, not a visible-runs prefix. "
            f"Use --run {hint} --task <task_id> or --task-dir <task-dir>. "
            f"Got: {value!r}"
        )
    return safe_id(raw, label)


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def parse_task_dir_arg(
    root: Path,
    task_dir_value: str,
    *,
    runs_root: Path | None = None,
) -> tuple[str, str, Path]:
    base = (runs_root or visible_runs_root(root)).resolve()
    raw = Path(task_dir_value)
    path = raw if raw.is_absolute() else (root / raw)
    try:
        path = path.resolve()
    except (OSError, ValueError) as exc:
        raise TaskStateError(f"task-dir is unreadable: {task_dir_value!r}") from exc
    if not path_is_relative_to(path, base):
        raise TaskStateError(
            f"task-dir escaped visible-runs: {task_dir_value!r}"
        )
    relative = path.relative_to(base)
    parts = relative.parts
    if len(parts) != 3 or parts[1] != "tasks":
        raise TaskStateError(
            "task-dir must be <visible-runs>/<run_id>/tasks/<task_id>. "
            f"Got: {task_dir_value!r}"
        )
    run = canonical_run_id(parts[0])
    task = safe_id(parts[2], "task")
    return run, task, path


def resolve_task_identity(
    root: Path,
    *,
    run: str | None,
    task: str | None,
    task_dir_value: str | None,
    fixture: str | None = None,
) -> tuple[str, str]:
    runs_root = runs_root_for(root, fixture)
    parsed_run = ""
    parsed_task = ""
    if task_dir_value:
        parsed_run, parsed_task, _path = parse_task_dir_arg(
            root,
            task_dir_value,
            runs_root=runs_root,
        )
    requested_run = canonical_run_id(run, label="run") if run else ""
    requested_task = safe_id(task, "task") if task else ""
    if parsed_run and requested_run and parsed_run != requested_run:
        raise TaskStateError(
            f"identity conflict: --run {requested_run!r} does not match "
            f"task-dir run_id {parsed_run!r}"
        )
    if parsed_task and requested_task and parsed_task != requested_task:
        raise TaskStateError(
            f"identity conflict: --task {requested_task!r} does not match "
            f"task-dir task_id {parsed_task!r}"
        )
    resolved_run = parsed_run or requested_run
    resolved_task = parsed_task or requested_task
    if not resolved_run or not resolved_task:
        raise TaskStateError(
            "--task-dir or both --run and --task are required; "
            "run_id is the simple id, task_dir is the path"
        )
    return resolved_run, resolved_task


def _acceptance_result_value(truth: dict[str, Any]) -> str:
    state = str(truth.get("protocol_terminal_state") or "")
    if state in VALID_TERMINAL_STATES:
        return "accepted" if truth.get("terminal_acceptance_accepted") else "rejected"
    if truth.get("terminal_acceptance_required"):
        return "pending"
    return "not_required"


def _terminal_end_timestamp(tdir: Path, truth: dict[str, Any]) -> datetime | None:
    for line in read_text(tdir / "events.jsonl").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("event") or "") != "task_signal_finish":
            continue
        parsed = parse_iso_timestamp(str(data.get("ts") or ""))
        if parsed is not None:
            return parsed
    claim = read_json(tdir / "terminal_output_claim.json")
    parsed = parse_iso_timestamp(str(claim.get("created_at") or ""))
    if parsed is not None:
        return parsed
    marker = str(truth.get("terminal_marker") or "")
    marker_ts = file_mtime(marker_path(tdir, marker)) if marker else None
    if marker_ts is not None:
        return datetime.fromtimestamp(marker_ts, timezone.utc)
    return None


def _duration_from_truth(tdir: Path, truth: dict[str, Any]) -> tuple[float | None, str]:
    if str(truth.get("protocol_terminal_state") or "") not in VALID_TERMINAL_STATES:
        return None, "unknown"
    task_json = read_json(tdir / "task.json")
    start = parse_iso_timestamp(str(task_json.get("created_at") or ""))
    end = _terminal_end_timestamp(tdir, truth)
    if start is None or end is None:
        return None, "unknown"
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return None, "unknown"
    return seconds, "known"


def build_result_receipt(truth: dict[str, Any], tdir: Path) -> dict[str, Any]:
    duration_seconds, duration_status = _duration_from_truth(tdir, truth)
    protocol_state = str(truth.get("protocol_terminal_state") or "")
    unknown_fields: list[str] = ["semantic_success"]
    semantic_success = "unknown"
    if duration_status == "unknown":
        unknown_fields.append("duration_seconds")
    lock_state = str(truth.get("lock_state") or "unknown")
    if not lock_state or lock_state == "unknown":
        unknown_fields.append("lock_state")
        lock_state = "unknown"
    return {
        "schema": RESULT_RECEIPT_SCHEMA,
        "run_id": str(truth.get("run_id") or ""),
        "task_id": str(truth.get("task_id") or ""),
        "task_dir": str(truth.get("task_dir") or ""),
        "report_path": str((truth.get("evidence_paths") or {}).get("report_md") or ""),
        "protocol_terminal_state": protocol_state,
        "protocol_complete": protocol_state in VALID_TERMINAL_STATES,
        "semantic_success": semantic_success,
        "acceptance_result": _acceptance_result_value(truth),
        "lock_state": lock_state,
        "duration_seconds": duration_seconds,
        "duration_status": duration_status,
        "unknown_fields": unknown_fields,
    }


def print_result_receipt(receipt: dict[str, Any]) -> None:
    print(f"result_receipt_schema: {receipt.get('schema') or RESULT_RECEIPT_SCHEMA}")
    print(f"run_id: {receipt.get('run_id') or '-'}")
    print(f"task_id: {receipt.get('task_id') or '-'}")
    print(f"task_dir: {receipt.get('task_dir') or '-'}")
    print(f"report_path: {receipt.get('report_path') or '-'}")
    print(f"protocol_terminal_state: {receipt.get('protocol_terminal_state') or '-'}")
    print(f"protocol_complete: {str(bool(receipt.get('protocol_complete'))).lower()}")
    print(f"semantic_success: {receipt.get('semantic_success') or 'unknown'}")
    print(f"acceptance_result: {receipt.get('acceptance_result') or 'unknown'}")
    print(f"lock_state: {receipt.get('lock_state') or 'unknown'}")
    duration = receipt.get("duration_seconds")
    print(
        "duration_seconds: "
        + ("unknown" if duration is None else str(duration))
    )
    print(f"duration_status: {receipt.get('duration_status') or 'unknown'}")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def quarantine_for_task(
    tdir: Path,
    run: str,
    task: str,
) -> dict[str, str]:
    path = tdir / "quarantine.json"
    data = read_json(path)
    reason = str(data.get("quarantine_reason") or "").strip()
    if (
        data.get("schema_version") != 1
        or data.get("quarantine_state") != "quarantined"
        or str(data.get("run_id") or "") != run
        or str(data.get("task_id") or "") != task
        or not reason
    ):
        return {}
    return {
        "quarantine_state": "quarantined",
        "quarantined_at": str(data.get("quarantined_at") or ""),
        "quarantine_reason": reason,
        "quarantine_worker_id": str(data.get("worker_id") or ""),
        "quarantine_archived_lock": str(data.get("archived_lock") or ""),
        "quarantine_path": str(path),
    }


def has_context_overflow_text(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CONTEXT_OVERFLOW_PHRASES)


def has_context_compaction_text(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CONTEXT_COMPACTION_PHRASES)


def int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def first_context_overflow_evidence(paths: list[Path], root: Path) -> dict[str, Any]:
    for path in paths:
        if not path.is_file():
            continue
        text = read_text(path)
        if not has_context_overflow_text(text):
            continue
        return {
            "context_overflow_detected": True,
            "context_overflow_evidence_path": rel(path, root),
            "context_overflow_evidence_source": path.name,
            "context_overflow_recoverable": True,
            "context_overflow_next_safe_action": CONTEXT_OVERFLOW_NEXT_SAFE_ACTION,
        }
    return {
        "context_overflow_detected": False,
        "context_overflow_evidence_path": "",
        "context_overflow_evidence_source": "",
        "context_overflow_recoverable": False,
        "context_overflow_next_safe_action": "",
    }


def provider_error_context_overflow_evidence(path: Path, root: Path) -> dict[str, Any]:
    data = read_json(path)
    current_attempt = str(
        read_json(path.parent / "acceptance_fence.json").get(
            "attempt_id"
        )
        or ""
    )
    if not (
        data.get("schema_version") == 1
        and data.get("source") == "opencode_session_message_api"
        and data.get("task_bound") is True
        and data.get("detected") is True
        and data.get("kind") == "context_window_overflow"
        and not data.get("resolved")
        and (
            not current_attempt
            or (
                str(data.get("attempt_id") or "") == current_attempt
                and data.get("evidence_scope") == "scoped"
                and data.get("confirmed") is True
            )
        )
    ):
        return {}
    return {
        "context_overflow_detected": True,
        "context_overflow_evidence_path": rel(path, root),
        "context_overflow_evidence_source": path.name,
        "context_overflow_recoverable": True,
        "context_overflow_next_safe_action": CONTEXT_OVERFLOW_NEXT_SAFE_ACTION,
    }


def provider_error_quarantine_eligible(data: dict[str, Any]) -> bool:
    return bool(
        data.get("schema_version") == 1
        and data.get("source") == "opencode_session_message_api"
        and data.get("task_bound") is True
        and data.get("detected") is True
        and (
            data.get("kind") == "upstream_request_timeout"
            or (
                data.get("kind") == "provider_finish_error"
                and data.get("message_finish") == "error"
                and data.get("retry_scope") == "fresh_session_only"
            )
        )
        and data.get("retryable") is False
        and str(data.get("session_id") or "").startswith("ses_")
        and int_value(data.get("message_completed_ms") or 0) > 0
        and (
            not data.get("attempt_id")
            or (
                data.get("evidence_scope") == "scoped"
                and data.get("confirmed") is True
                and not data.get("resolved")
            )
        )
    )


def provider_error_continuation_eligible(data: dict[str, Any]) -> bool:
    return bool(
        data.get("schema_version") == 1
        and data.get("source") == "opencode_session_message_api"
        and data.get("task_bound") is True
        and data.get("detected") is True
        and data.get("kind") == "provider_finish_error"
        and data.get("retryable") is False
        and str(data.get("session_id") or "").startswith("ses_")
        and data.get("message_finish") == "error"
        and int_value(data.get("message_completed_ms") or 0) > 0
        and int_value(data.get("message_token_total") or 0) == 0
        and int_value(data.get("message_token_output") or 0) == 0
        and not data.get("structured_error_present")
        and int_value(data.get("response_text_chars") or 0) == 0
        and not data.get("output_degeneration_detected")
    )


def background_child_failure_continuation_eligible(
    data: dict[str, Any],
) -> bool:
    return bool(
        data.get("schema_version") == 1
        and data.get("source") == "opencode_background_output"
        and data.get("task_bound") is True
        and data.get("detected") is True
        and data.get("kind") == "background_child_finish_error"
        and data.get("retryable") is False
        and data.get("confirmed") is True
        and str(data.get("session_id") or "").startswith("ses_")
        and data.get("raw_status") == "error"
        and data.get("completion_seen") is True
        and data.get("output_present") is False
        and int_value(data.get("output_chars") or 0) == 0
        and data.get("evidence_scope") == "scoped"
        and data.get("classification") == "background_child_terminal_error"
        and not data.get("resolved")
    )


def session_rotation_requirement_path(
    root: Path,
    worker_id: str,
) -> Path:
    safe_id(worker_id, "worker")
    return (
        framework_root(root)
        / "session-rotation-required"
        / f"{worker_id}.json"
    )


def record_session_rotation_requirement_locked(
    root: Path,
    worker_id: str,
    *,
    run: str,
    task: str,
    provider_error: dict[str, Any],
) -> dict[str, Any]:
    """Record a contamination fence while the visible registry lock is held."""
    path = session_rotation_requirement_path(root, worker_id)
    source_session_id = str(
        provider_error.get("session_id") or ""
    )
    if not source_session_id.startswith("ses_"):
        raise TaskStateError(
            "session rotation requirement needs a task-bound source session"
        )
    existing = read_json(path)
    if existing.get("status") == "required":
        return existing
    requirement = {
        "schema_version": 1,
        "fence_id": str(uuid.uuid4()),
        "status": "required",
        "worker_id": worker_id,
        "source_run_id": run,
        "source_task_id": task,
        "source_session_id": source_session_id,
        "provider_error_kind": str(
            provider_error.get("kind") or ""
        ),
        "rotation_reason": "context_contaminated",
        "required_at": utc_now(),
        "path": str(path),
    }
    write_json_atomic(path, requirement)
    return requirement


def record_provider_error_rotation_requirement_locked(
    root: Path,
    worker_id: str,
    *,
    run: str,
    task: str,
    provider_error: dict[str, Any],
) -> dict[str, Any]:
    if not (
        str(provider_error.get("run") or "") == run
        and str(provider_error.get("task") or "") == task
        and str(provider_error.get("worker_id") or "") == worker_id
        and provider_error_quarantine_eligible(provider_error)
    ):
        return {}
    return record_session_rotation_requirement_locked(
        root,
        worker_id,
        run=run,
        task=task,
        provider_error=provider_error,
    )


PROVIDER_ERROR_OBSERVATION_SOURCES = {
    "opencode_session_message_api",
    "tmux_pane_capture",
    "opencode_background_output",
}


def provider_error_observation_state(
    data: dict[str, Any],
    *,
    run: str,
    task: str,
    current_attempt: str,
    record_present: bool,
    fence_worker: str,
) -> str:
    """#975: name why detected=false was projected; false is never health."""
    if not data:
        return "record_unreadable" if record_present else "observation_missing"
    if (
        data.get("schema_version") != 1
        or str(data.get("source") or "") not in PROVIDER_ERROR_OBSERVATION_SOURCES
    ):
        return "record_invalid"
    record_run = str(data.get("run") or "")
    record_task = str(data.get("task") or "")
    record_worker = str(data.get("worker_id") or "")
    if (
        (run and record_run and record_run != run)
        or (task and record_task and record_task != task)
        or (
            fence_worker
            and record_worker
            and record_worker != fence_worker
        )
    ):
        return "foreign_record"
    if current_attempt and str(data.get("attempt_id") or "") != current_attempt:
        return "not_current_attempt"
    if data.get("resolved"):
        return "resolved"
    if data.get("detected") is True:
        if data.get("task_bound") is not True:
            return "not_task_bound"
        if data.get("evidence_scope") != "scoped":
            return "unscoped_evidence"
        return "record_invalid"
    if data.get("detected") is not False:
        # #975: only an exact boolean False supports a no-error snapshot;
        # missing/null/0/"false" are value-shape damage, never health.
        return "record_invalid"
    if data.get("task_bound") is not True or (
        data.get("evidence_scope") == "insufficient"
    ):
        return "insufficient_evidence"
    return "no_error_observed"


def provider_error_evidence(path: Path, root: Path) -> dict[str, Any]:
    data = read_json(path)
    source = str(data.get("source") or "")
    kind = str(data.get("kind") or "")
    fence = read_json(path.parent / "acceptance_fence.json")
    current_attempt = str(fence.get("attempt_id") or "")
    fence_worker = str(fence.get("worker_id") or "")
    task = path.parent.name if path.parent.parent.name == "tasks" else ""
    run = path.parent.parent.parent.name if task else ""
    observed_at = str(data.get("observed_at") or "")
    attempt_bound = not (
        current_attempt
        and str(data.get("attempt_id") or "") != current_attempt
    )
    foreign = bool(
        (
            run
            and str(data.get("run") or "")
            and str(data.get("run")) != run
        )
        or (
            task
            and str(data.get("task") or "")
            and str(data.get("task")) != task
        )
        or (
            fence_worker
            and str(data.get("worker_id") or "")
            and str(data.get("worker_id")) != fence_worker
        )
    )
    if not (
        data.get("schema_version") == 1
        and source in PROVIDER_ERROR_OBSERVATION_SOURCES
        and not foreign
        and data.get("task_bound") is True
        and data.get("detected") is True
        and kind
        and not data.get("resolved")
        and attempt_bound
        and (
            not current_attempt
            or data.get("evidence_scope") == "scoped"
        )
    ):
        schema1 = data.get("schema_version") == 1 and (
            source in PROVIDER_ERROR_OBSERVATION_SOURCES
        )
        blank = {
            "provider_error_detected": False,
            "provider_error_kind": "",
            "provider_error_retryable": False,
            "provider_error_confirmed": False,
            "provider_error_attempt_id": (
                str(data.get("attempt_id") or "") if schema1 else ""
            ),
            "provider_error_evidence_scope": "",
            "provider_error_request_id": "",
            "provider_error_status": "",
            "provider_error_source": source if schema1 else "",
            "provider_error_evidence_path": "",
            "provider_error_next_safe_action": "",
            "provider_error_retry_scope": "",
            "provider_error_message_id": "",
            "provider_error_message_completed_ms": 0,
            "provider_error_message_finish": "",
            "provider_error_message_reason": "",
            "provider_error_message_token_total": 0,
            "provider_error_message_token_output": 0,
            "provider_error_structured_error_present": False,
            "provider_error_response_text_chars": 0,
            "provider_error_attach_token_count": 0,
            "provider_error_output_degeneration_detected": False,
            "provider_error_quarantine_eligible": False,
            "provider_error_continuation_eligible": False,
            "background_child_failure_continuation_eligible": False,
            "provider_error_observed_at": observed_at,
            "provider_error_observation_state": (
                provider_error_observation_state(
                    data,
                    run=run,
                    task=task,
                    current_attempt=current_attempt,
                    record_present=path.is_file(),
                    fence_worker=fence_worker,
                )
            ),
        }
        record_status = str(data.get("status") or "")
        if (
            data.get("schema_version") == 1
            and source in PROVIDER_ERROR_OBSERVATION_SOURCES
            and not foreign
            and not data.get("resolved")
            and attempt_bound
            and record_status
        ):
            blank["provider_error_status"] = record_status
            blank["provider_error_evidence_path"] = rel(path, root)
            if (
                source == "opencode_session_message_api"
                and data.get("detected") is False
                and record_status == "session_message_payload_too_large"
            ):
                # #823: keep payload-cap insufficiency visible but
                # non-authoritative.
                blank["provider_error_payload_limit_bytes"] = int_value(
                    data.get("payload_limit_bytes") or 0
                )
                blank["provider_error_next_safe_action"] = (
                    PAYLOAD_EXCEEDED_NEXT_SAFE_ACTION
                )
        return blank
    retryable = bool(data.get("retryable"))
    quarantine_eligible = provider_error_quarantine_eligible(data)
    continuation_eligible = provider_error_continuation_eligible(data)
    background_continuation_eligible = (
        background_child_failure_continuation_eligible(data)
    )
    both_continuation_eligible = (
        continuation_eligible or background_continuation_eligible
    )
    return {
        "provider_error_detected": True,
        "provider_error_kind": kind,
        "provider_error_retryable": retryable,
        "provider_error_confirmed": bool(
            data.get("confirmed")
            if current_attempt
            else True
        ),
        "provider_error_attempt_id": str(
            data.get("attempt_id") or ""
        ),
        "provider_error_evidence_scope": str(
            data.get("evidence_scope") or ""
        ),
        "provider_error_request_id": str(data.get("request_id") or ""),
        "provider_error_status": str(data.get("status") or ""),
        "provider_error_source": source,
        "provider_error_evidence_path": rel(path, root),
        "provider_error_retry_scope": str(
            data.get("retry_scope") or ""
        ),
        "provider_error_message_id": str(
            data.get("message_id") or ""
        ),
        "provider_error_message_completed_ms": int_value(
            data.get("message_completed_ms") or 0
        ),
        "provider_error_message_finish": str(
            data.get("message_finish") or ""
        ),
        "provider_error_message_reason": str(
            data.get("message_reason") or ""
        ),
        "provider_error_message_token_total": int_value(
            data.get("message_token_total") or 0
        ),
        "provider_error_message_token_output": int_value(
            data.get("message_token_output") or 0
        ),
        "provider_error_structured_error_present": bool(
            data.get("structured_error_present")
        ),
        "provider_error_response_text_chars": int_value(
            data.get("response_text_chars") or 0
        ),
        "provider_error_attach_token_count": int_value(
            data.get("attach_token_count") or 0
        ),
        "provider_error_output_degeneration_detected": bool(
            data.get("output_degeneration_detected")
        ),
        "provider_error_quarantine_eligible": quarantine_eligible,
        "provider_error_continuation_eligible": both_continuation_eligible,
        "background_child_failure_continuation_eligible": (
            background_continuation_eligible
        ),
        "provider_error_next_safe_action": (
            PROVIDER_QUOTA_NEXT_SAFE_ACTION
            if kind == "provider_spending_limit"
            else (
                "owner_preserving_recovery_handshake"
                if both_continuation_eligible
                else (
                    "manual_continue_required"
                    if retryable
                    else (
                        "manual_operator_review_then_quarantine_and_rotate"
                        if quarantine_eligible
                        else "collect_task_bound_session_provider_evidence"
                    )
                )
            )
        ),
        "provider_error_observed_at": observed_at,
        "provider_error_observation_state": "current_attempt",
    }


def blank_verbal_terminal_evidence() -> dict[str, Any]:
    return {
        "verbal_terminal_sentinel_detected": False,
        "verbal_terminal_sentinel_status": "",
        "verbal_terminal_sentinel_state": "",
        "verbal_terminal_sentinel_line": "",
        "verbal_terminal_sentinel_evidence_path": "",
        "verbal_terminal_sentinel_message_id": "",
        "verbal_terminal_sentinel_next_safe_action": "",
    }


def verbal_terminal_evidence(path: Path, root: Path, task: str) -> dict[str, Any]:
    data = read_json(path)
    state = str(data.get("sentinel_state") or "").strip()
    line = str(data.get("sentinel_line") or "").strip()
    current_attempt = str(
        read_json(path.parent / "acceptance_fence.json").get(
            "attempt_id"
        )
        or ""
    )
    expected_line = (
        f"OMX_TASK_CLOSED {task} {current_attempt} .{state}"
        if current_attempt
        else f"OMX_TASK_CLOSED {task} {state}"
    )
    if not (
        data.get("schema_version") == 1
        and data.get("source") == "opencode_session_message_api"
        and data.get("task_bound") is True
        and data.get("detected") is True
        and data.get("kind") == VERBAL_TERMINAL_STATUS
        and str(data.get("task_id") or data.get("task") or "") == task
        and state in TERMINAL_MARKERS
        and line == expected_line
        and (
            not current_attempt
            or (
                str(data.get("attempt_id") or "") == current_attempt
                and data.get("evidence_scope") == "scoped"
            )
        )
    ):
        return blank_verbal_terminal_evidence()
    return {
        "verbal_terminal_sentinel_detected": True,
        "verbal_terminal_sentinel_status": VERBAL_TERMINAL_STATUS,
        "verbal_terminal_sentinel_state": state,
        "verbal_terminal_sentinel_line": line,
        "verbal_terminal_sentinel_evidence_path": rel(path, root),
        "verbal_terminal_sentinel_message_id": str(data.get("message_id") or ""),
        "verbal_terminal_sentinel_next_safe_action": VERBAL_TERMINAL_NEXT_SAFE_ACTION,
    }


def blank_context_compaction_evidence() -> dict[str, Any]:
    return {
        "context_compaction_detected": False,
        "context_compaction_evidence_path": "",
        "context_compaction_evidence_source": "",
        "context_compaction_next_safe_action": "",
        "context_compaction_observed_kind": "",
        "context_compaction_classification": "",
        "context_compaction_session_id": "",
        "context_compaction_pruned_count": 0,
        "context_compaction_tool_part_compacted_count": 0,
        "context_compaction_message_api_near_phase_timestamp_count": 0,
        "context_compaction_session_compacted_event_observed": False,
        "context_compaction_summary_compaction_observed": False,
    }


def structured_context_compaction_evidence(path: Path, root: Path) -> dict[str, Any]:
    data = read_json(path)
    if not data:
        return blank_context_compaction_evidence()

    message_api = data.get("message_api")
    if not isinstance(message_api, dict):
        message_api = {}
    log_data = data.get("log")
    if not isinstance(log_data, dict):
        log_data = {}
    proof = data.get("message_api_compacted_proof")
    if not isinstance(proof, dict):
        proof = {}

    observed_kind = str(data.get("observed_kind") or data.get("kind") or "")
    classification = str(data.get("classification") or data.get("observation_classification") or "")
    session_id = str(data.get("session_id") or proof.get("session_id") or "")
    pruned_count = int_value(
        log_data.get("pruned_count")
        or data.get("pruned_count")
        or proof.get("pruned_count")
    )
    tool_part_compacted_count = int_value(
        message_api.get("tool_part_compacted_count")
        or data.get("tool_part_compacted_count")
        or proof.get("state_time_compacted_count")
    )
    near_phase_timestamp_count = int_value(
        message_api.get("near_phase_timestamp_count")
        or data.get("near_phase_timestamp_count")
        or proof.get("near_phase41d_timestamp_count")
        or proof.get("near_phase_timestamp_count")
    )
    session_compacted_event_observed = bool(data.get("session_compacted_event_observed"))
    summary_compaction_observed = bool(
        data.get("summary_compaction_observed")
        or data.get("llm_summary_compaction_observed")
    )

    observed_text = f"{observed_kind} {classification}".lower()
    positive_text_signal = any(
        token in observed_text
        for token in (
            "automatic_prune",
            "session_compacted",
            "session.compacted",
            "summary_compaction",
            "llm_summary",
        )
    )
    detected = bool(
        positive_text_signal
        or pruned_count > 0
        or session_compacted_event_observed
        or summary_compaction_observed
    )
    if not detected:
        return blank_context_compaction_evidence()

    return {
        "context_compaction_detected": True,
        "context_compaction_evidence_path": rel(path, root),
        "context_compaction_evidence_source": path.name,
        "context_compaction_next_safe_action": CONTEXT_COMPACTION_NEXT_SAFE_ACTION,
        "context_compaction_observed_kind": observed_kind,
        "context_compaction_classification": classification,
        "context_compaction_session_id": session_id,
        "context_compaction_pruned_count": pruned_count,
        "context_compaction_tool_part_compacted_count": tool_part_compacted_count,
        "context_compaction_message_api_near_phase_timestamp_count": near_phase_timestamp_count,
        "context_compaction_session_compacted_event_observed": session_compacted_event_observed,
        "context_compaction_summary_compaction_observed": summary_compaction_observed,
    }


def first_context_compaction_evidence(paths: list[Path], root: Path) -> dict[str, Any]:
    for path in paths:
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            structured = structured_context_compaction_evidence(path, root)
            if structured["context_compaction_detected"]:
                return structured
        text = read_text(path)
        if not has_context_compaction_text(text):
            continue
        evidence = blank_context_compaction_evidence()
        evidence.update({
            "context_compaction_detected": True,
            "context_compaction_evidence_path": rel(path, root),
            "context_compaction_evidence_source": path.name,
            "context_compaction_next_safe_action": CONTEXT_COMPACTION_NEXT_SAFE_ACTION,
        })
        return evidence
    return blank_context_compaction_evidence()


def file_nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime if path.exists() else None
    except OSError:
        return None


def iso_from_ts(value: float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def timestamp_after(candidate: str, baseline: str) -> bool:
    if not candidate or not baseline:
        return bool(candidate and not baseline)
    parsed_candidate = parse_iso_timestamp(candidate)
    parsed_baseline = parse_iso_timestamp(baseline)
    if parsed_candidate is not None and parsed_baseline is not None:
        return parsed_candidate > parsed_baseline
    return candidate > baseline


def marker_path(tdir: Path, name: str) -> Path:
    return tdir / f".{name}"


def runs_root_for(root: Path, fixture: str | None = None) -> Path:
    return fixture_runs_root(root, fixture) if fixture else visible_runs_root(root)


def task_dir(root: Path, run: str, task: str, *, runs_root: Path | None = None) -> Path:
    canonical_run_id(run)
    safe_id(task, "task")
    base = runs_root or visible_runs_root(root)
    return base / run / "tasks" / task


def owner_backend_for_task(tdir: Path) -> tuple[str, str]:
    owner = read_text(tdir / "owner").strip()
    backend = read_text(tdir / "backend").strip()
    task_json = read_json(tdir / "task.json")
    dispatch = read_json(tdir / "dispatch.json")
    owner = owner or str(task_json.get("owner") or task_json.get("worker") or dispatch.get("worker") or "")
    backend = backend or str(task_json.get("backend") or dispatch.get("backend") or "")
    return owner, backend


def parsed_timestamp_values(data: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("ts", "updated_at", "last_ts", "attempted_at", "submitted_at", "failed_at", "created_at", "recovered_at"):
        value = data.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    return values


def newest_event_timestamp(paths: list[Path]) -> str:
    timestamps: list[str] = []
    mtimes: list[float] = []
    for path in paths:
        if path.suffix == ".jsonl" and path.is_file():
            for line in read_text(path).splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    timestamps.extend(parsed_timestamp_values(data))
        elif path.is_file():
            data = read_json(path)
            if data:
                timestamps.extend(parsed_timestamp_values(data))
        mtime = file_mtime(path)
        if mtime is not None:
            mtimes.append(mtime)
    if timestamps:
        return max(timestamps)
    if mtimes:
        return iso_from_ts(max(mtimes))
    return ""


def newest_mtime_path(paths: list[Path]) -> Path | None:
    existing = [(file_mtime(path), path) for path in paths if path.is_file()]
    existing = [(mtime, path) for mtime, path in existing if mtime is not None]
    if not existing:
        return None
    return max(existing, key=lambda item: item[0] or 0)[1]


def reviewer_progress_sidecar_paths(tdir: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in REVIEWER_PROGRESS_SIDECAR_GLOBS:
        paths.extend(sorted(tdir.glob(pattern)))
    return paths


def active_lock_for_task(root: Path, run: str, task: str, *, runs_root: Path | None = None) -> dict[str, Any]:
    base = runs_root or visible_runs_root(root)
    lock_root = locks_root_for_runs(base)
    if not lock_root.is_dir():
        return {}
    for lock_file in sorted(lock_root.glob("*.lock.json")):
        data = read_json(lock_file)
        if str(data.get("run") or "") == run and str(data.get("task") or "") == task:
            return {
                "path": lock_file,
                "data": data,
            }
    return {}


def truth_for_task(root: Path, run: str, task: str, *, runs_root: Path | None = None) -> dict[str, Any]:
    base = runs_root or visible_runs_root(root)
    tdir = task_dir(root, run, task, runs_root=base)
    if not tdir.is_dir():
        raise TaskStateError(f"task directory missing: {rel(tdir, root)}")

    report = tdir / "report.md"
    protocol_status = tdir / "protocol_status.json"
    events = tdir / "events.jsonl"
    delivery = tdir / "delivery.json"
    delivery_error = tdir / "delivery_error.json"
    submit_interrupted = tdir / "submit_interrupted.json"
    submit_error = tdir / "submit_error.json"
    provider_error_observation = tdir / "provider_error_observation.json"
    quarantine_path = tdir / "quarantine.json"
    compaction_observation = tdir / CONTEXT_COMPACTION_OBSERVATION_FILES[0]
    terminal_sentinel_observation = tdir / TERMINAL_SENTINEL_OBSERVATION_FILES[0]
    summary_json = base / run / "summary.json"
    summary_md = base / run / "summary.md"

    overflow_evidence = provider_error_context_overflow_evidence(provider_error_observation, root) or first_context_overflow_evidence(
        [protocol_status, events, delivery, delivery_error, submit_error, report],
        root,
    )
    provider_error = provider_error_evidence(provider_error_observation, root)
    compaction_evidence = first_context_compaction_evidence(
        [compaction_observation, protocol_status, events, delivery, delivery_error, submit_error, report],
        root,
    )
    verbal_terminal = verbal_terminal_evidence(terminal_sentinel_observation, root, task)
    context_overflow_detected = bool(overflow_evidence["context_overflow_detected"])
    context_compaction_detected = bool(compaction_evidence["context_compaction_detected"])
    verbal_terminal_detected = bool(verbal_terminal["verbal_terminal_sentinel_detected"])
    quarantine = quarantine_for_task(tdir, run, task)
    quarantine_valid = bool(quarantine)
    terminal_acceptance = terminal_acceptance_status(tdir)

    terminal_markers = [name for name in TERMINAL_MARKERS if marker_path(tdir, name).is_file()]
    flow_markers = [name for name in FLOW_MARKERS if marker_path(tdir, name).is_file()]
    terminal_marker_count = len(terminal_markers)
    terminal_marker = terminal_markers[0] if len(terminal_markers) == 1 else ""
    report_bytes = file_size(report)
    report_nonempty = report_bytes > 0
    terminal_marker_mtime = (
        file_mtime(marker_path(tdir, terminal_marker))
        if terminal_marker
        else None
    )
    terminal_marker_age_seconds = (
        max(0.0, datetime.now(timezone.utc).timestamp() - terminal_marker_mtime)
        if terminal_marker_mtime is not None
        else None
    )
    terminal_report_settle_pending = bool(
        terminal_marker_count == 1
        and not report_nonempty
        and terminal_marker_age_seconds is not None
        and terminal_marker_age_seconds <= TERMINAL_REPORT_SETTLE_SECONDS
    )
    has_submitted = "submitted" in flow_markers
    has_received = "received" in flow_markers
    has_started = "started" in flow_markers
    has_heartbeat = "heartbeat" in flow_markers

    if terminal_marker_count > 1:
        protocol_terminal_state = "invalid_conflicting_terminal_markers"
        state_reason = "multiple terminal markers exist"
    elif terminal_marker_count == 1:
        if terminal_report_settle_pending:
            protocol_terminal_state = "terminal_marker_pending_report"
            state_reason = (
                f".{terminal_marker} observed; waiting up to "
                f"{TERMINAL_REPORT_SETTLE_SECONDS}s for non-empty report.md"
            )
        elif not report_nonempty:
            protocol_terminal_state = "invalid_terminal_no_report"
            state_reason = f".{terminal_marker} without non-empty report.md"
        elif not terminal_acceptance["accepted"]:
            protocol_terminal_state = "invalid_terminal_acceptance_fence"
            state_reason = str(terminal_acceptance["reason"])
        elif terminal_marker == "done":
            protocol_terminal_state = "completed"
            state_reason = "report.md plus .done"
        elif terminal_marker == "fail":
            protocol_terminal_state = "failed"
            state_reason = "report.md plus .fail"
        else:
            protocol_terminal_state = "blocked"
            state_reason = "report.md plus .blocked"
    elif (
        provider_error["provider_error_detected"]
        and provider_error["provider_error_confirmed"]
        and not provider_error["provider_error_retryable"]
        and provider_error["provider_error_kind"]
        != "context_window_overflow"
        and has_submitted
    ):
        protocol_terminal_state = "blocked_provider_error"
        state_reason = (
            "non-retryable provider error detected after submission; "
            "preserve ownership and follow the projected safe action: "
            f"{provider_error['provider_error_next_safe_action']}"
        )
    elif context_compaction_detected:
        protocol_terminal_state = CONTEXT_COMPACTION_STATUS
        state_reason = "OpenCode context compaction evidence detected without terminal truth; manual operator review required"
    elif context_overflow_detected:
        protocol_terminal_state = CONTEXT_OVERFLOW_STATUS
        state_reason = "OpenCode context window overflow detected after submission; manual operator review required"
    elif verbal_terminal_detected:
        protocol_terminal_state = VERBAL_TERMINAL_STATUS
        state_reason = "visible OMX_TASK_CLOSED line detected without report.md plus terminal marker; manual operator review required"
    elif submit_interrupted.is_file():
        protocol_terminal_state = "delivery_uncertain"
        state_reason = (
            "submit_interrupted.json exists before delivery outcome was "
            "classified"
        )
    elif delivery_error.is_file() or submit_error.is_file():
        protocol_terminal_state = "submit_failed"
        state_reason = "delivery_error.json or submit_error.json exists"
    elif (
        provider_error["provider_error_detected"]
        and provider_error["provider_error_retryable"]
        and has_submitted
        and not has_received
    ):
        protocol_terminal_state = "delivery_uncertain_provider_error"
        state_reason = (
            ".submitted exists without .received and a retryable provider error "
            f"was observed: {provider_error['provider_error_kind']}"
        )
    elif has_started or has_heartbeat:
        protocol_terminal_state = "running"
        state_reason = ".started or .heartbeat exists"
    elif has_received:
        protocol_terminal_state = "received"
        state_reason = ".received exists without terminal marker"
    elif has_submitted:
        protocol_terminal_state = "delivery_uncertain"
        state_reason = ".submitted exists without .received"
    else:
        protocol_terminal_state = "prepared"
        state_reason = "task metadata exists without lifecycle markers"

    lock_doc = active_lock_for_task(root, run, task, runs_root=base)
    has_active_lock = bool(lock_doc)
    lock_is_terminal_stale = bool(has_active_lock and protocol_terminal_state in VALID_TERMINAL_STATES)
    lock_state = "stale_terminal_lock" if lock_is_terminal_stale else ("active_lock" if has_active_lock else "none")

    owner, backend = owner_backend_for_task(tdir)
    summary_data = read_json(summary_json)
    previous_summary_state = str(summary_data.get("protocol_terminal_state") or summary_data.get("terminal_state") or "")

    event_paths = [
        events,
        protocol_status,
        delivery,
        delivery_error,
        submit_interrupted,
        submit_error,
        provider_error_observation,
        quarantine_path,
    ]
    marker_paths = [marker_path(tdir, name) for name in TERMINAL_MARKERS + FLOW_MARKERS]
    reviewer_sidecar_paths = reviewer_progress_sidecar_paths(tdir)
    protocol_freshness_ts = newest_event_timestamp(event_paths + [report] + marker_paths)
    latest_sidecar_ts = newest_event_timestamp(reviewer_sidecar_paths)
    latest_sidecar_path = newest_mtime_path(reviewer_sidecar_paths)
    sidecar_progress_recent = timestamp_after(latest_sidecar_ts, protocol_freshness_ts)
    last_event_ts = newest_event_timestamp(event_paths + [report] + marker_paths + reviewer_sidecar_paths)
    evidence_paths = {
        "task_dir": rel(tdir, root),
        "report_md": rel(report, root),
        "protocol_status_json": rel(protocol_status, root),
        "events_jsonl": rel(events, root),
        "delivery_json": rel(delivery, root),
        "delivery_error_json": rel(delivery_error, root),
        "submit_interrupted_json": rel(submit_interrupted, root),
        "submit_error_json": rel(submit_error, root),
        "provider_error_observation_json": rel(provider_error_observation, root),
        "quarantine_json": rel(quarantine_path, root),
        "acceptance_fence_json": rel(tdir / "acceptance_fence.json", root),
        "terminal_output_claim_json": rel(
            tdir / "terminal_output_claim.json",
            root,
        ),
        "context_compaction_observed_json": rel(compaction_observation, root),
        "terminal_sentinel_observation_json": rel(terminal_sentinel_observation, root),
        "summary_json": rel(summary_json, root),
        "summary_md": rel(summary_md, root),
        "lock_json": rel(lock_doc["path"], root) if lock_doc else "",
    }

    context_overflow_status = ""
    if context_overflow_detected:
        if protocol_terminal_state in VALID_TERMINAL_STATES:
            context_overflow_status = "recovered_terminal_with_context_overflow_evidence"
        else:
            context_overflow_status = CONTEXT_OVERFLOW_STATUS
    context_compaction_status = ""
    if context_compaction_detected:
        if protocol_terminal_state in VALID_TERMINAL_STATES:
            context_compaction_status = "recovered_terminal_with_context_compaction_evidence"
        else:
            context_compaction_status = CONTEXT_COMPACTION_STATUS

    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(root),
        "runs_root": rel(base, root),
        "run_id": run,
        "task_id": task,
        "task_dir": rel(tdir, root),
        "owner": owner,
        "backend": backend,
        "protocol_terminal_state": protocol_terminal_state,
        "state_reason": state_reason,
        "report_bytes": report_bytes,
        "report_nonempty": report_nonempty,
        "terminal_marker": terminal_marker,
        "terminal_marker_count": terminal_marker_count,
        "terminal_markers": terminal_markers,
        "terminal_marker_age_seconds": terminal_marker_age_seconds,
        "terminal_report_settle_seconds": TERMINAL_REPORT_SETTLE_SECONDS,
        "terminal_report_settle_pending": terminal_report_settle_pending,
        "terminal_acceptance_required": bool(terminal_acceptance["required"]),
        "terminal_acceptance_accepted": bool(terminal_acceptance["accepted"]),
        "terminal_protocol_classification": str(
            terminal_acceptance.get("protocol_classification") or ""
        ),
        "terminal_acceptance_reason": str(terminal_acceptance["reason"]),
        "terminal_acceptance_mismatched_fields": list(
            terminal_acceptance["mismatched_fields"]
        ),
        "incomplete_terminal_closeout": bool(
            protocol_terminal_state == "invalid_terminal_acceptance_fence"
        ),
        "terminal_recovery_action": (
            TERMINAL_RECOVERY_ACTION
            if protocol_terminal_state == "invalid_terminal_acceptance_fence"
            else ""
        ),
        "flow_markers": flow_markers,
        "has_submitted": has_submitted,
        "has_received": has_received,
        "has_started": has_started,
        "has_heartbeat": has_heartbeat,
        "submit_interrupted": submit_interrupted.is_file(),
        "has_active_lock": has_active_lock,
        "lock_state": lock_state,
        "lock_is_terminal_stale": lock_is_terminal_stale,
        "lock_path": evidence_paths["lock_json"],
        "lock_data": lock_doc.get("data", {}) if lock_doc else {},
        "last_event_ts": last_event_ts,
        "protocol_freshness_ts": protocol_freshness_ts,
        "sidecar_progress_recent": sidecar_progress_recent,
        "latest_sidecar_ts": latest_sidecar_ts,
        "latest_sidecar_path": rel(latest_sidecar_path, root) if latest_sidecar_path else "",
        "sidecar_progress_paths": [rel(path, root) for path in reviewer_sidecar_paths],
        "previous_summary_state": previous_summary_state,
        "summary_exists": summary_json.is_file(),
        "summary_data": summary_data,
        "evidence_paths": evidence_paths,
        **overflow_evidence,
        **provider_error,
        **compaction_evidence,
        **verbal_terminal,
        "context_overflow_status": context_overflow_status,
        "context_compaction_status": context_compaction_status,
        "quarantine_state": quarantine.get("quarantine_state", ""),
        "quarantine_projection_status": (
            "quarantined_nonterminal"
            if quarantine_valid
            and not has_active_lock
            and protocol_terminal_state not in VALID_TERMINAL_STATES
            else ""
        ),
        "quarantine_reason": quarantine.get("quarantine_reason", ""),
        "quarantined_at": quarantine.get("quarantined_at", ""),
        "quarantine_worker_id": quarantine.get("quarantine_worker_id", ""),
        "quarantine_archived_lock": quarantine.get(
            "quarantine_archived_lock",
            "",
        ),
        "quarantine_path": (
            rel(Path(quarantine["quarantine_path"]), root) if quarantine else ""
        ),
    }
    payload["result_receipt"] = build_result_receipt(payload, tdir)
    return payload


def archive_lock_file(root: Path, run: str, task: str, reason: str, *, runs_root: Path | None = None) -> dict[str, str]:
    truth = truth_for_task(root, run, task, runs_root=runs_root)
    if not truth.get("has_active_lock"):
        return {"archived": "", "lock_path": ""}
    if not truth.get("lock_is_terminal_stale"):
        raise TaskStateError(f"refusing to archive non-terminal lock: {truth.get('protocol_terminal_state')}")
    tdir = task_dir(root, run, task, runs_root=runs_root or visible_runs_root(root))
    authority = pin_terminal_authority(tdir)
    if not authority.get("ok"):
        return {
            "archived": "",
            "lock_path": str(truth.get("lock_path") or ""),
            "blocked_reason": str(authority.get("reason") or ""),
        }
    lock_rel = str(truth.get("lock_path") or "")
    lock_file = root / lock_rel
    expected = dict(truth.get("lock_data") or {})
    expected_fence = owner_lock_transition_fence(expected)
    with visible_registry_lock(root):
        data = read_json(lock_file)
        actual_fence = owner_lock_transition_fence(data)
        if actual_fence != expected_fence:
            raise TaskStateError(
                "owner lock identity changed"
                f" before archive: expected={expected_fence!r}"
                f" actual={actual_fence!r}"
            )
        now = utc_now()
        data["status"] = "released"
        data["released_at"] = now
        data["release_reason"] = reason
        worker = safe_id(str(data.get("worker_id") or "unknown"), "worker")
        if runs_root is None:
            provider_error = read_json(
                task_dir(root, run, task)
                / "provider_error_observation.json"
            )
            record_provider_error_rotation_requirement_locked(
                root,
                worker,
                run=run,
                task=task,
                provider_error=provider_error,
            )
        safe_run = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in run
        )
        safe_task = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in task
        )
        base = runs_root or visible_runs_root(root)
        archive = lock_archive_root_for_runs(base) / (
            f"{worker}.{safe_run}.{safe_task}."
            f"{now.replace(':', '').replace('-', '')}.lock.json"
        )
        try:
            durable_atomic_write(
                archive,
                json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n",
            )
        except OSError as exc:
            raise TaskStateError(
                f"owner lock archive was not durable before release: {exc}"
            ) from exc
        lock_file.unlink(missing_ok=True)
        try:
            fsync_directory(lock_file.parent)
        except OSError as exc:
            raise TaskStateError(
                "owner lock was removed but the removal is not confirmed "
                f"durable: {exc}; a stale terminal lock may reappear and "
                "must be settled through the existing idempotent stale-lock "
                "handling"
            ) from exc
        return {
            "archived": rel(archive, root),
            "lock_path": rel(lock_file, root),
        }


def infer_single_fixture_run_task(root: Path, fixture: str) -> tuple[str, str]:
    base = fixture_runs_root(root, fixture)
    runs = [path for path in sorted(base.iterdir()) if path.is_dir() and path.name != "_locks"] if base.is_dir() else []
    candidates: list[tuple[str, str]] = []
    for run_dir in runs:
        tasks_dir = run_dir / "tasks"
        if not tasks_dir.is_dir():
            continue
        for tdir in sorted(path for path in tasks_dir.iterdir() if path.is_dir()):
            candidates.append((run_dir.name, tdir.name))
    if len(candidates) != 1:
        raise TaskStateError(f"fixture {fixture!r} must contain exactly one task when --run/--task are omitted")
    return candidates[0]


def render_human(truth: dict[str, Any]) -> str:
    lines = [
        f"run_id: {truth.get('run_id')}",
        f"task_id: {truth.get('task_id')}",
        f"task_dir: {truth.get('task_dir') or '-'}",
        f"owner: {truth.get('owner') or '-'}",
        f"backend: {truth.get('backend') or '-'}",
        f"protocol_terminal_state: {truth.get('protocol_terminal_state')}",
        f"state_reason: {truth.get('state_reason')}",
        f"report_bytes: {truth.get('report_bytes')}",
        f"terminal_marker: {truth.get('terminal_marker') or '-'}",
        f"terminal_marker_count: {truth.get('terminal_marker_count')}",
        f"has_received: {truth.get('has_received')}",
        f"has_started: {truth.get('has_started')}",
        f"has_heartbeat: {truth.get('has_heartbeat')}",
        f"has_active_lock: {truth.get('has_active_lock')}",
        f"lock_state: {truth.get('lock_state')}",
        f"lock_is_terminal_stale: {truth.get('lock_is_terminal_stale')}",
        f"incomplete_terminal_closeout: {str(bool(truth.get('incomplete_terminal_closeout'))).lower()}",
        f"terminal_recovery_action: {truth.get('terminal_recovery_action') or '-'}",
        f"previous_summary_state: {truth.get('previous_summary_state') or '-'}",
        f"last_event_ts: {truth.get('last_event_ts') or '-'}",
    ]
    observation_state = str(
        truth.get("provider_error_observation_state") or ""
    )
    if observation_state:
        if truth.get("provider_error_detected") is True:
            observation_note = (
                "confirmed_error"
                if truth.get("provider_error_confirmed") is True
                else "detected_error_unconfirmed"
            )
        elif observation_state == "no_error_observed":
            # #975: a no-error snapshot is bounded history, not a
            # real-time health claim.
            observation_note = (
                "snapshot_limited_history_not_realtime_health"
            )
        elif observation_state == "observation_missing":
            observation_note = "not_yet_observed"
        else:
            observation_note = "unknown"
        lines.extend([
            f"provider_error_observation_state: {observation_state}",
            "provider_error_observed_at: "
            + (str(truth.get("provider_error_observed_at") or "") or "unknown"),
            f"provider_error_observation_note: {observation_note}",
            f"provider_error_status: {truth.get('provider_error_status') or '-'}",
            f"provider_error_evidence_path: {truth.get('provider_error_evidence_path') or '-'}",
        ])
    receipt = truth.get("result_receipt")
    if isinstance(receipt, dict):
        lines.extend([
            f"protocol_complete: {str(bool(receipt.get('protocol_complete'))).lower()}",
            f"semantic_success: {receipt.get('semantic_success') or 'unknown'}",
            f"acceptance_result: {receipt.get('acceptance_result') or 'unknown'}",
            "duration_seconds: "
            + (
                "unknown"
                if receipt.get("duration_seconds") is None
                else str(receipt.get("duration_seconds"))
            ),
            f"duration_status: {receipt.get('duration_status') or 'unknown'}",
        ])
    if truth.get("context_overflow_detected"):
        lines.extend([
            f"context_overflow_status: {truth.get('context_overflow_status') or '-'}",
            f"context_overflow_evidence_source: {truth.get('context_overflow_evidence_source') or '-'}",
            f"context_overflow_evidence_path: {truth.get('context_overflow_evidence_path') or '-'}",
            f"context_overflow_next_safe_action: {truth.get('context_overflow_next_safe_action') or '-'}",
        ])
    if truth.get("context_compaction_detected"):
        lines.extend([
            f"context_compaction_status: {truth.get('context_compaction_status') or '-'}",
            f"context_compaction_evidence_source: {truth.get('context_compaction_evidence_source') or '-'}",
            f"context_compaction_evidence_path: {truth.get('context_compaction_evidence_path') or '-'}",
            f"context_compaction_next_safe_action: {truth.get('context_compaction_next_safe_action') or '-'}",
        ])
        if truth.get("context_compaction_observed_kind"):
            lines.extend([
                f"context_compaction_observed_kind: {truth.get('context_compaction_observed_kind')}",
                f"context_compaction_classification: {truth.get('context_compaction_classification') or '-'}",
                f"context_compaction_tool_part_compacted_count: {truth.get('context_compaction_tool_part_compacted_count')}",
                f"context_compaction_pruned_count: {truth.get('context_compaction_pruned_count')}",
                (
                    "context_compaction_message_api_near_phase_timestamp_count: "
                    f"{truth.get('context_compaction_message_api_near_phase_timestamp_count')}"
                ),
                (
                    "context_compaction_session_compacted_event_observed: "
                    f"{truth.get('context_compaction_session_compacted_event_observed')}"
                ),
                (
                    "context_compaction_summary_compaction_observed: "
                    f"{truth.get('context_compaction_summary_compaction_observed')}"
                ),
            ])
    if truth.get("verbal_terminal_sentinel_detected"):
        lines.extend([
            f"verbal_terminal_sentinel_status: {truth.get('verbal_terminal_sentinel_status') or '-'}",
            f"verbal_terminal_sentinel_state: {truth.get('verbal_terminal_sentinel_state') or '-'}",
            f"verbal_terminal_sentinel_line: {truth.get('verbal_terminal_sentinel_line') or '-'}",
            f"verbal_terminal_sentinel_evidence_path: {truth.get('verbal_terminal_sentinel_evidence_path') or '-'}",
            f"verbal_terminal_sentinel_next_safe_action: {truth.get('verbal_terminal_sentinel_next_safe_action') or '-'}",
        ])
    if truth.get("quarantine_state"):
        lines.extend([
            f"quarantine_state: {truth.get('quarantine_state')}",
            f"quarantine_projection_status: {truth.get('quarantine_projection_status') or '-'}",
            f"quarantine_reason: {truth.get('quarantine_reason') or '-'}",
            f"quarantined_at: {truth.get('quarantined_at') or '-'}",
            f"quarantine_archived_lock: {truth.get('quarantine_archived_lock') or '-'}",
        ])
    evidence = truth.get("evidence_paths", {})
    if isinstance(evidence, dict):
        lines.append("evidence_paths:")
        for key, value in evidence.items():
            if value:
                lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def render_usage_human(payload: dict[str, Any]) -> str:
    identity = payload.get("identity") or {}
    summary = payload.get("session_summary") or {}
    tokens = summary.get("tokens") or {}
    lines = [
        f"schema: {payload.get('schema')}",
        f"run_id: {identity.get('run_id') or '-'}",
        f"task_id: {identity.get('task_id') or '-'}",
        f"task_dir: {identity.get('task_dir') or '-'}",
        f"attempt_id: {identity.get('attempt_id') or '-'}",
        f"attribution: {payload.get('attribution') or '-'}",
        f"session_summary.user_messages: {summary.get('user_messages')}",
        f"session_summary.assistant_messages: {summary.get('assistant_messages')}",
    ]
    for name in ("input", "output", "reasoning", "cache_read", "cache_write"):
        bucket = tokens.get(name) or {}
        total = bucket.get("sum")
        lines.append(
            f"tokens.{name}: sum=" + ("unknown" if total is None else str(total))
            + f" status={bucket.get('status') or '-'}"
            + f" covered={bucket.get('covered_messages')}"
            + f" missing={bucket.get('missing_messages')}"
            + f" invalid={bucket.get('invalid_messages')}"
        )
    lines.append(
        "session_summary.error_nonnull_assistant_messages: "
        f"{summary.get('error_nonnull_assistant_messages')}"
    )
    models = summary.get("models") or []
    lines.append(f"session_summary.models: {', '.join(models) if models else '-'}")
    for item in payload.get("selection") or []:
        tokens_in = (item.get("tokens") or {}).get("input") or {}
        total = tokens_in.get("sum")
        lines.append(
            f"selection.request_id {item.get('request_id')}: "
            f"assistant_messages={item.get('assistant_messages')}"
            + " input_sum=" + ("unknown" if total is None else str(total))
        )
    lines.append(f"actual_cost: {payload.get('actual_cost') or 'unknown'}")
    lines.append(f"child_usage: {payload.get('child_usage') or '-'}")
    note = payload.get("attribution_note")
    if note:
        lines.append(f"attribution_note: {note}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omx_visible_task_state",
        description=(
            "Return deterministic protocol truth and a stable result receipt "
            "for a visible task. run_id is the simple identifier; task_dir is "
            "the path. Dispatch output of those fields can be queried here "
            "without guessing a visible-runs prefix."
        ),
        epilog=(
            "Query examples: omx_visible_task_state --root <root> --run <run_id> "
            "--task <task_id> --json   OR   --task-dir <task_dir> --json. "
            "Wrong ids, mixed identities, and paths outside visible-runs fail "
            "closed without writing. Protocol complete is not semantic success. "
            "duration_seconds is task.json created_at to the first "
            "task_signal_finish event, else terminal_output_claim.created_at, "
            "else the terminal marker mtime; later sidecars do not change it. "
            "Generic dispatch does not judge business/semantic success."
        ),
    )
    parser.add_argument("--root", default=None)
    parser.add_argument(
        "--receipt",
        help="saved run receipt or observation_json object; refresh that exact task/attempt read-only",
    )
    parser.add_argument(
        "--resume-context",
        help="with --receipt: explicit Main-owned task anchor JSON with decision path and current evidence identity",
    )
    parser.add_argument("--run", help="simple run_id, not visible-runs/<run_id>")
    parser.add_argument("--task", help="simple task_id")
    parser.add_argument(
        "--task-dir",
        dest="task_dir",
        help="path <visible-runs>/<run_id>/tasks/<task_id>; preferred public query entry",
    )
    parser.add_argument("--fixture", help="read from .omx/framework/team/finalizer-fixtures/<fixture>/visible-runs")
    projection_group = parser.add_mutually_exclusive_group()
    projection_group.add_argument(
        "--checkpoints",
        action="store_true",
        help="return the compact advisory progress-checkpoint projection instead of protocol truth",
    )
    projection_group.add_argument(
        "--usage",
        action="store_true",
        help=(
            "return the formal read-only task usage projection for the exact "
            "fence-bound session (requires --db and --attempt-id)"
        ),
    )
    parser.add_argument(
        "--since-seq",
        dest="since_seq",
        type=int,
        default=None,
        help="with --checkpoints: only records after this cursor seq",
    )
    parser.add_argument(
        "--attempt-id",
        dest="attempt_id",
        default="",
        help=(
            "with --checkpoints/--usage: the caller's explicit expected attempt; it "
            "must match the authoritative acceptance-fence/task identity"
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "with --usage: path to the OpenCode SQLite message store; opened "
            "read-only, never discovered automatically"
        ),
    )
    parser.add_argument(
        "--request-id",
        dest="request_id",
        action="append",
        default=[],
        help=(
            "with --usage: operator-selected user message id inside the exact "
            "fence-bound session; repeatable, duplicates are ignored"
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _resume_context_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise TaskStateError(f"{label} exceeds 1 MiB")
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise TaskStateError(f"{label} unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskStateError(f"{label} must be a JSON object")
    return value


def _main_decision_resume(
    root: Path, context_path: str, receipt: dict[str, Any], attempt: str, owner: str,
) -> dict[str, Any]:
    path = Path(context_path)
    if not path.is_absolute():
        path = root / path
    context = _resume_context_json(path, "resume context")
    if context.get("schema") != "visible-task-resume-context/v1":
        raise TaskStateError("unsupported resume context schema")
    expected = {"run_id": receipt["run_id"], "task_id": receipt["task_id"],
                "attempt_id": attempt, "worker_id": owner}
    for field, value in expected.items():
        if context.get(field) != value:
            raise TaskStateError(f"resume context {field} identity mismatch")
    for field in ("evidence_version", "main_decision_path"):
        if not isinstance(context.get(field), str) or not context[field].strip() or context[field] == "unknown":
            raise TaskStateError(f"resume context {field} must be explicit")
    digest = context.get("report_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise TaskStateError("resume context report_sha256 must be a SHA-256 hex digest")
    for field in ("remaining_requirements", "completed_delivery_evidence"):
        values = context.get(field)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise TaskStateError(f"resume context {field} must be a list of nonempty strings")
    try:
        decision_path = Path(context["main_decision_path"])
        if not decision_path.is_absolute():
            decision_path = path.parent / decision_path
        decision = _resume_context_json(decision_path, "Main decision")
    except TaskStateError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            decision = {}
        else:
            raise
    state = "unknown"
    reason = "missing_or_unsupported_Main_decision"
    if receipt["protocol_complete"]:
        report_path = root / receipt["report_path"]
        try:
            with report_path.open("rb") as stream:
                current_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise TaskStateError(f"current report unreadable: {report_path}: {exc}") from exc
        if digest != current_digest:
            raise TaskStateError("resume context report_sha256 does not match current report evidence")
        if decision.get("schema") == "visible-task-main-decision/v1":
            for field, value in {**expected, "evidence_version": context["evidence_version"],
                                 "report_sha256": digest}.items():
                if decision.get(field) != value:
                    raise TaskStateError(f"Main decision {field} mismatch; inspect the exact decision and current evidence")
            if decision.get("decided_by") == "Main" and decision.get("decision") in (
                "accepted", "needs_changes", "rejected",
            ):
                state = decision["decision"]
                reason = "explicit_Main_decision_for_current_evidence"
    else:
        reason = "protocol_incomplete"
    return {
        "resume_context_path": str(path), "main_decision_path": str(decision_path),
        "main_content_decision": state, "main_decision_reason": reason,
        "evidence_version": context["evidence_version"], "report_sha256": digest,
        "unfinished_requirements": ([] if state == "accepted" else ["main_content_acceptance"])
        + context["remaining_requirements"],
        "completed_delivery_evidence": context["completed_delivery_evidence"],
    }


def truth_from_saved_receipt(
    root: Path, receipt_path: str, resume_context_path: str | None = None,
) -> dict[str, Any]:
    path = Path(receipt_path)
    if not path.is_absolute():
        path = root / path
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise TaskStateError("saved receipt exceeds 1 MiB; select the compact receipt object")
        saved = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise TaskStateError(f"saved receipt unreadable: {path}: {exc}") from exc
    if not isinstance(saved, dict):
        raise TaskStateError("saved receipt must be a JSON object")
    observation = saved.get("observe") is True and saved.get("schema_version") == 1
    if not observation and saved.get("schema") not in (
        "visible-orchestrate-run-receipt/v1", "visible-operator-dispatch-receipt/v1",
    ):
        raise TaskStateError("unsupported saved receipt; use run --receipt-out or the observation_json object")

    def identity_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip() or value.strip() == "unknown":
            raise TaskStateError(f"saved receipt {label} is unknown; inspect this receipt's exact task/claim path")
        return value.strip()

    run = identity_text(saved.get("run" if observation else "run_id"), "run_id")
    task = identity_text(saved.get("task" if observation else "task_id"), "task_id")
    grant = saved.get("grant") if isinstance(saved.get("grant"), dict) else {}
    attempt = identity_text(saved.get("attempt_id") or grant.get("attempt_id"), "attempt_id")
    if grant.get("attempt_id") not in (None, "", attempt):
        raise TaskStateError("saved receipt grant attempt_id conflict")
    if saved.get("task_dir") is not None and not isinstance(saved["task_dir"], str):
        raise TaskStateError("saved receipt task_dir must be a path string")
    run, task = resolve_task_identity(
        root, run=run, task=task, task_dir_value=saved.get("task_dir"),
    )
    if saved.get("identity_task_id") not in (None, "", task):
        raise TaskStateError("saved receipt identity_task_id conflict")
    if observation and (
        saved.get("expected_attempt_id") != attempt
        or saved.get("identity_guard_ok") is not True
        or saved.get("identity_conflict") is not False
    ):
        raise TaskStateError("saved observation identity guard refused; inspect the exact task fence")
    prior = saved.get("result_receipt")
    if isinstance(prior, dict):
        if prior.get("task_dir") is not None and not isinstance(prior["task_dir"], str):
            raise TaskStateError("saved result_receipt task_dir must be a path string")
        resolve_task_identity(root, run=run, task=task, task_dir_value=prior.get("task_dir"))
        if prior.get("run_id") != run or prior.get("task_id") != task:
            raise TaskStateError("saved result_receipt identity conflict")
    tdir = task_dir(root, run, task)
    if not tdir.is_dir():
        raise TaskStateError(f"task directory missing: {tdir}")
    access = read_checkpoint_access(root, tdir)
    identity = access["identity"]
    if not access.get("ok"):
        raise TaskStateError(f"saved receipt current identity guard refused: {access.get('status')}")
    if identity.get("attempt_id") != attempt:
        raise TaskStateError("saved receipt attempt_id does not match the current task attempt")
    saved_owner = saved.get("owner" if observation else "worker_id")
    if saved.get("schema") == "visible-operator-dispatch-receipt/v1" and saved_owner is None:
        saved_owner = identity.get("worker")
    owner = identity_text(saved_owner, "worker_id")
    if owner != identity.get("worker"):
        raise TaskStateError("saved receipt worker_id does not match the current task owner")

    cursor = 0
    continuation = saved.get("continuation") if observation else None
    if observation:
        cursor = saved.get("cursor")
        if type(cursor) is not int or cursor < 0:
            raise TaskStateError("saved observation cursor must be a nonnegative integer")
        if continuation is not None and (
            not isinstance(continuation, dict)
            or continuation.get("last_confirmed_cursor") != cursor
        ):
            raise TaskStateError("saved observation confirmed cursor conflict")
    truth = truth_for_task(root, run, task)
    receipt = truth["result_receipt"]
    command: list[str] = []
    if receipt["protocol_complete"]:
        action = "read_report_then_main_content_decision"
    elif observation and (
        saved.get("wait_result") != "observation_deadline"
        or any(saved.get(key) for key in (
            "notable_checkpoint", "records", "critical_beyond_return", "pending_after_return",
            "foreign_or_stale_records", "checkpoint_invalid_records", "unsafe_evidence_records",
            "checkpoint_incomplete_tail", "checkpoint_out_of_order", "checkpoint_oversize_store",
        ))
    ):
        action = "inspect_exact_task_evidence"
    elif truth["protocol_terminal_state"] in {"running", "received"} and not truth.get("provider_error_detected"):
        action = "observe_same_attempt"
        command = [str(root / "bin/omx_visible_dispatch"), "wait", "--root", str(root),
                   "--run", run, "--task", task, "--observe", "--compact",
                   "--attempt-id", attempt, "--since-seq", str(cursor)]
        if isinstance(continuation, dict):
            for field, flag in (("segment_timeout", "--timeout"), ("total_timeout", "--total-timeout")):
                budget = continuation.get(field)
                if type(budget) is not int or budget <= 0:
                    raise TaskStateError(f"saved observation {field} must be a positive integer")
                command.extend([flag, str(budget)])
    else:
        action = "inspect_exact_task_evidence"
    delivery = read_json(tdir / "delivery.json")
    live_observation = delivery.get("live_session_observation") or {}
    if not receipt["protocol_complete"] and isinstance(live_observation, dict) and (
        live_observation.get("status") == "live_session_mismatch"
        and delivery.get("post_delivery_session_observation_ok") is False
    ):
        action = "inspect_exact_task_session_identity_mismatch"
        command = []
    payload = {key: truth[key] for key in (
        "run_id", "task_id", "task_dir", "owner", "protocol_terminal_state",
        "state_reason", "report_nonempty", "report_bytes", "lock_state", "result_receipt",
    )}
    payload["receipt_resume"] = {
        "receipt_path": str(path), "attempt_id": attempt, "worker_id": owner,
        "cursor": cursor, "next_action": action,
        "next_observe_command": shlex.join(command) if command else "",
    }
    if resume_context_path is not None:
        resume = payload["receipt_resume"]
        resume.update(_main_decision_resume(root, resume_context_path, receipt, attempt, owner))
        if resume["main_content_decision"] == "accepted":
            resume["next_action"] = ("continue_unfinished_requirements" if resume["unfinished_requirements"]
                                     else "no_unfinished_requirements")
    return payload


def checkpoint_projection(
    root: Path,
    run: str,
    task: str,
    *,
    runs_root: Path | None = None,
    since_seq: int,
    attempt_id: str,
    notify_names: list[str] | None = None,
) -> dict[str, Any]:
    base = runs_root or visible_runs_root(root)
    tdir = task_dir(root, run, task, runs_root=base)
    if not tdir.is_dir():
        raise TaskStateError(f"task directory missing: {rel(tdir, root)}")
    expected_attempt = str(attempt_id or "").strip()
    if not expected_attempt:
        raise TaskStateError(
            "checkpoint query requires --attempt-id: reads are bound to the "
            "caller's explicit expected attempt"
        )
    store = read_progress_checkpoints(tdir)
    access = read_checkpoint_access(root, tdir)
    identity = access["identity"]
    if identity.get("known") and str(identity.get("attempt_id") or "") != expected_attempt:
        raise TaskStateError(
            f"checkpoint query attempt {expected_attempt} does not match the "
            f"current task attempt {identity.get('attempt_id') or '-'}"
        )
    guard_ok = bool(access.get("ok"))
    identity_records, foreign_count = identity_valid_records(store, identity)
    current_records = identity_records if guard_ok else []
    page = earliest_page(current_records, since_seq) if guard_ok else []
    cursor = since_seq if guard_ok and not page else latest_seq(page)
    pending_after_page = [
        record
        for record in current_records
        if int(record.get("seq", 0)) > max(since_seq, cursor)
    ]
    notable_page = [
        {"seq": record.get("seq"), "reason": reason}
        for record in page
        for notable, reason in [classify_notable(record, notify_names=notify_names)]
        if notable
    ]
    critical_beyond = [
        int(record.get("seq", 0))
        for record in pending_after_page
        for notable, _reason in [classify_notable(record, notify_names=notify_names)]
        if notable
    ]
    return {
        "schema_version": 1,
        "run": run,
        "task": task,
        "task_dir": rel(tdir, root),
        "identity_known": bool(identity.get("known")),
        "identity_source": str(identity.get("source") or ""),
        "identity_conflict": bool(identity.get("conflict")),
        "attempt_id": str(identity.get("attempt_id") or ""),
        "expected_attempt_id": expected_attempt,
        "identity_guard_ok": guard_ok,
        "identity_guard_mode": str(access.get("mode") or ""),
        "identity_guard_status": str(access.get("status") or ""),
        "withheld_records": 0 if guard_ok else len(identity_records),
        "progress_unknown": (not current_records) or not guard_ok,
        "checkpoint_cursor": cursor,
        "store_latest_seq": store_latest_seq(store),
        "checkpoint_count": len(current_records),
        "checkpoint_invalid_lines": store["invalid_count"],
        "checkpoint_incomplete_tail": store["incomplete_tail"],
        "checkpoint_out_of_order": store["out_of_order"],
        "checkpoint_limit_reached": store["limit_reached"],
        "foreign_or_stale_records": foreign_count,
        "unsafe_evidence_records": store["unsafe_evidence_count"],
        "since_seq": since_seq,
        "pending_count": len(
            [
                record
                for record in current_records
                if int(record.get("seq", 0)) > since_seq
            ]
        ),
        "pending_after_page": len(pending_after_page),
        "pending_notable": notable_page,
        "critical_beyond_page": critical_beyond,
        "records": page,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    usage_requested = bool(getattr(args, "usage", False))
    request_ids = list(getattr(args, "request_id", []) or [])
    expected_attempt = str(getattr(args, "attempt_id", "") or "").strip()
    if args.resume_context and not args.receipt:
        parser.error("--resume-context requires --receipt")
    if args.receipt and any((args.run, args.task, args.task_dir, args.fixture, args.checkpoints,
                             usage_requested, args.since_seq is not None, expected_attempt, args.db,
                             request_ids)):
        parser.error("--receipt supplies the exact identity and cannot be mixed with other query selectors")
    if usage_requested:
        if args.fixture is not None:
            parser.error("--usage does not accept --fixture (checkpoint-specific mode)")
        if args.since_seq is not None:
            parser.error("--usage does not accept --since-seq (checkpoint-specific)")
        if not args.db:
            parser.error("--usage requires an explicit non-empty --db")
        if not expected_attempt:
            parser.error(
                "usage query requires --attempt-id: reads are bound to the "
                "caller's explicit expected attempt"
            )
    else:
        if args.db is not None:
            parser.error("--db is only valid with --usage")
        if request_ids:
            parser.error("--request-id is only valid with --usage")
    try:
        root = root_path(args.root)
        if args.receipt:
            truth = truth_from_saved_receipt(root, args.receipt, args.resume_context)
            if args.json:
                print(json.dumps(truth, indent=2, sort_keys=True, ensure_ascii=False))
            else:
                for key, value in truth.items():
                    if key not in {"result_receipt", "receipt_resume"}:
                        print(f"{key}: {value}")
                print_result_receipt(truth["result_receipt"])
                for key, value in truth["receipt_resume"].items():
                    print(f"{key}: {value}")
            return 0
        run = args.run
        task = args.task
        if args.fixture and (not run or not task) and not args.task_dir:
            run, task = infer_single_fixture_run_task(root, args.fixture)
        run, task = resolve_task_identity(
            root,
            run=run,
            task=task,
            task_dir_value=args.task_dir,
            fixture=args.fixture,
        )
        if usage_requested:
            tdir = task_dir(root, run, task)
            if not tdir.is_dir():
                raise TaskStateError(f"task directory missing: {rel(tdir, root)}")
            payload = build_usage_payload(
                task_dir=tdir,
                db_path=str(args.db),
                attempt_id=expected_attempt,
                request_ids=request_ids,
                run_id=run,
                task_id=task,
            )
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
            else:
                print(render_usage_human(payload), end="")
            return 0
        if getattr(args, "checkpoints", False):
            projection = checkpoint_projection(
                root,
                run,
                task,
                runs_root=runs_root_for(root, args.fixture),
                since_seq=max(0, int(getattr(args, "since_seq", 0) or 0)),
                attempt_id=str(getattr(args, "attempt_id", "") or ""),
            )
            if args.json:
                print(json.dumps(projection, indent=2, sort_keys=True, ensure_ascii=False))
            else:
                print(f"run: {projection['run']}")
                print(f"task: {projection['task']}")
                print(f"identity_known: {str(projection['identity_known']).lower()}")
                print(f"identity_source: {projection['identity_source'] or '-'}")
                print(f"identity_conflict: {str(projection['identity_conflict']).lower()}")
                print(f"attempt_id: {projection['attempt_id'] or '-'}")
                print(f"expected_attempt_id: {projection['expected_attempt_id'] or '-'}")
                print(f"identity_guard_ok: {str(projection['identity_guard_ok']).lower()}")
                print(f"identity_guard_mode: {projection['identity_guard_mode'] or '-'}")
                print(f"identity_guard_status: {projection['identity_guard_status'] or '-'}")
                print(f"withheld_records: {projection['withheld_records']}")
                print(f"progress_unknown: {str(projection['progress_unknown']).lower()}")
                print(f"checkpoint_cursor: {projection['checkpoint_cursor']}")
                print(f"store_latest_seq: {projection['store_latest_seq']}")
                print(f"checkpoint_count: {projection['checkpoint_count']}")
                print(f"checkpoint_invalid_lines: {projection['checkpoint_invalid_lines']}")
                print(f"checkpoint_incomplete_tail: {str(projection['checkpoint_incomplete_tail']).lower()}")
                print(f"checkpoint_out_of_order: {str(projection['checkpoint_out_of_order']).lower()}")
                print(f"foreign_or_stale_records: {projection['foreign_or_stale_records']}")
                print(f"unsafe_evidence_records: {projection['unsafe_evidence_records']}")
                print(f"since_seq: {projection['since_seq']}")
                print(f"pending_count: {projection['pending_count']}")
                print(f"pending_after_page: {projection['pending_after_page']}")
                for item in projection["pending_notable"]:
                    print(f"pending_notable: seq={item['seq']} reason={item['reason']}")
                for seq in projection["critical_beyond_page"]:
                    print(f"critical_beyond_page: seq={seq}")
                for record in projection["records"]:
                    print(
                        "checkpoint: "
                        + json.dumps(record, sort_keys=True, ensure_ascii=False)
                    )
            return 0
        truth = truth_for_task(root, run, task, runs_root=runs_root_for(root, args.fixture))
        if args.json:
            print(json.dumps(truth, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            print(render_human(truth), end="")
        return 0
    except TaskUsageError as exc:
        detail = json.dumps(exc.detail, sort_keys=True, ensure_ascii=False)
        print(f"ERROR: {exc.code}: {detail}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"ERROR: db_read_failed: {exc}", file=sys.stderr)
        return 1
    except TaskStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
