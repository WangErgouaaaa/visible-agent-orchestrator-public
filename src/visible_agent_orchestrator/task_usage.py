"""Formal read-only task usage query for the visible task-state CLI.

Standard-library-only, exact-session SQLite reads against the verified
OpenCode 1.18.29 message-table shape. Ported from the accepted P5
experiment (worker-progress-correction-20260919/p5-live/task_usage.py).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

USAGE_SCHEMA = "visible-task-usage/v1"
STORAGE_SCOPE = "opencode-1.18.29-verified-message-table-shape-only"
REQUIRED_FENCE_KEYS = (
    "attempt_id",
    "session_id",
    "task_id",
    "worker_id",
    "instance_id",
    "run_id",
)
REQUIRED_MESSAGE_COLUMNS = ("id", "session_id", "data", "time_created")
TOKEN_FIELDS = (
    ("input", "$.tokens.input"),
    ("output", "$.tokens.output"),
    ("reasoning", "$.tokens.reasoning"),
    ("cache_read", "$.tokens.cache.read"),
    ("cache_write", "$.tokens.cache.write"),
)

ASSISTANT_SELECT = """
SELECT
  id,
  json_extract(data, '$.parentID'),
  json_extract(data, '$.tokens.input'),
  json_type(data, '$.tokens.input'),
  json_extract(data, '$.tokens.output'),
  json_type(data, '$.tokens.output'),
  json_extract(data, '$.tokens.reasoning'),
  json_type(data, '$.tokens.reasoning'),
  json_extract(data, '$.tokens.cache.read'),
  json_type(data, '$.tokens.cache.read'),
  json_extract(data, '$.tokens.cache.write'),
  json_type(data, '$.tokens.cache.write'),
  time_created,
  json_extract(data, '$.time.completed'),
  json_extract(data, '$.modelID'),
  json_extract(data, '$.providerID'),
  (json_extract(data, '$.error') IS NOT NULL)
FROM message
WHERE session_id = ? AND json_extract(data, '$.role') = 'assistant'
"""

USER_COUNT_SELECT = (
    "SELECT COUNT(*) FROM message "
    "WHERE session_id = ? AND json_extract(data, '$.role') = 'user'"
)


class TaskUsageError(Exception):
    """Expected operator-facing usage-query failure."""

    def __init__(self, code: str, detail: dict[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _new_buckets() -> dict[str, dict[str, int]]:
    return {
        name: {"sum": 0, "covered": 0, "missing": 0, "invalid": 0}
        for name, _ in TOKEN_FIELDS
    }


def _accumulate(buckets: dict[str, dict[str, int]], values: tuple, jtypes: tuple) -> None:
    for (name, _), value, jtype in zip(TOKEN_FIELDS, values, jtypes):
        bucket = buckets[name]
        if jtype is None or jtype == "null":
            bucket["missing"] += 1
        elif jtype == "integer" and value >= 0:
            bucket["sum"] += value
            bucket["covered"] += 1
        else:
            bucket["invalid"] += 1


def _bucket_report(scope_messages: int, bucket: dict[str, int]) -> dict[str, Any]:
    if bucket["covered"] == 0:
        status = "unknown"
        total = None
    elif bucket["missing"] or bucket["invalid"]:
        status = "partial"
        total = bucket["sum"]
    else:
        status = "complete"
        total = bucket["sum"]
    return {
        "sum": total,
        "status": status,
        "covered_messages": bucket["covered"],
        "missing_messages": bucket["missing"],
        "invalid_messages": bucket["invalid"],
        "scope_messages": scope_messages,
    }


def _tokens_report(scope_messages: int, buckets: dict[str, dict[str, int]]) -> dict[str, Any]:
    return {
        name: _bucket_report(scope_messages, buckets[name])
        for name, _ in TOKEN_FIELDS
    }


def _min_or_none(values: list):
    try:
        return min(values) if values else None
    except TypeError:
        return None


def _max_or_none(values: list):
    try:
        return max(values) if values else None
    except TypeError:
        return None


def _time_report(created: list, completed: list) -> dict[str, Any]:
    return {
        "created_min": _min_or_none(created),
        "completed_max": _max_or_none(completed),
        "created_present": len(created),
        "completed_present": len(completed),
    }


def _aggregate(rows: list) -> dict[str, Any]:
    session_buckets = _new_buckets()
    per_parent: dict[str, dict[str, Any]] = {}
    models: set[str] = set()
    error_rows = 0
    created: list = []
    completed: list = []
    for row in rows:
        values, jtypes = row[2:12:2], row[3:12:2]
        _accumulate(session_buckets, values, jtypes)
        parent = row[1]
        entry = per_parent.setdefault(
            parent,
            {
                "buckets": _new_buckets(),
                "rows": 0,
                "models": set(),
                "errors": 0,
                "created": [],
                "completed": [],
            },
        )
        entry["rows"] += 1
        _accumulate(entry["buckets"], values, jtypes)
        model_id, provider_id = row[14], row[15]
        if model_id is not None and provider_id is not None:
            label = f"{provider_id}/{model_id}"
            models.add(label)
            entry["models"].add(label)
        if row[16]:
            error_rows += 1
            entry["errors"] += 1
        if row[12] is not None:
            created.append(row[12])
            entry["created"].append(row[12])
        if row[13] is not None:
            completed.append(row[13])
            entry["completed"].append(row[13])
    return {
        "rows": rows,
        "session_buckets": session_buckets,
        "per_parent": per_parent,
        "models": sorted(models),
        "error_rows": error_rows,
        "created": created,
        "completed": completed,
    }


def load_usage_fence(task_dir: Path) -> dict[str, Any]:
    path = Path(task_dir) / "acceptance_fence.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskUsageError("fence_unreadable", {"path": str(path), "reason": str(exc)})
    try:
        fence = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskUsageError("fence_invalid_json", {"path": str(path), "reason": str(exc)})
    if not isinstance(fence, dict):
        raise TaskUsageError("fence_invalid_shape", {"path": str(path)})
    missing = [
        key
        for key in REQUIRED_FENCE_KEYS
        if not isinstance(fence.get(key), str) or not fence.get(key)
    ]
    if missing:
        raise TaskUsageError("fence_missing_keys", {"path": str(path), "missing": missing})
    return fence


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise TaskUsageError("db_not_found", {"path": str(path)})
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise TaskUsageError("db_open_failed", {"path": str(path), "reason": str(exc)})
    conn.execute("PRAGMA query_only=ON")
    return conn


def _check_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'message'"
    ).fetchone()
    if row is None:
        raise TaskUsageError("schema_missing_table", {"table": "message"})
    columns = {info[1] for info in conn.execute("PRAGMA table_info(message)")}
    missing = [name for name in REQUIRED_MESSAGE_COLUMNS if name not in columns]
    if missing:
        raise TaskUsageError(
            "schema_missing_columns", {"table": "message", "missing": missing}
        )


def _resolve_request_ids(
    conn: sqlite3.Connection, session_id: str, request_ids: list[str]
) -> tuple[list[str], list[str]]:
    duplicates = sorted(
        rid for rid, count in Counter(request_ids).items() if count > 1
    )
    unique = list(dict.fromkeys(request_ids))
    rejected: list[dict[str, str]] = []
    selected: list[str] = []
    for rid in unique:
        if not isinstance(rid, str) or not rid:
            rejected.append({"request_id": str(rid), "reason": "empty_request_id"})
            continue
        row = conn.execute(
            "SELECT json_extract(data, '$.role') FROM message "
            "WHERE session_id = ? AND id = ?",
            (session_id, rid),
        ).fetchone()
        if row is None:
            rejected.append(
                {"request_id": rid, "reason": "not_found_in_target_session"}
            )
        elif row[0] != "user":
            rejected.append({"request_id": rid, "reason": "not_a_user_message"})
        else:
            selected.append(rid)
    if rejected:
        raise TaskUsageError(
            "request_id_rejected",
            {
                "rejected": rejected,
                "note": (
                    "IDs are only accepted as user messages inside the exact "
                    "fence-bound session; cross-session lookups are not performed"
                ),
            },
        )
    return selected, duplicates


def _selection_entry(request_id: str, entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {
            "request_id": request_id,
            "assistant_messages": 0,
            "tokens": _tokens_report(0, _new_buckets()),
            "error_nonnull_assistant_messages": 0,
            "models": [],
            "time": _time_report([], []),
        }
    return {
        "request_id": request_id,
        "assistant_messages": entry["rows"],
        "tokens": _tokens_report(entry["rows"], entry["buckets"]),
        "error_nonnull_assistant_messages": entry["errors"],
        "models": sorted(entry["models"]),
        "time": _time_report(entry["created"], entry["completed"]),
    }


def build_usage_payload(
    *,
    task_dir: Path,
    db_path: str,
    attempt_id: str,
    request_ids: list[str],
    run_id: str,
    task_id: str,
) -> dict[str, Any]:
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise TaskUsageError(
            "invalid_arguments", {"detail": "--attempt-id must be non-empty"}
        )
    fence = load_usage_fence(task_dir)
    if fence["attempt_id"] != attempt_id:
        raise TaskUsageError(
            "attempt_mismatch",
            {
                "fence_attempt_id": fence["attempt_id"],
                "given_attempt_id": attempt_id,
            },
        )
    if fence["run_id"] != run_id or fence["task_id"] != task_id:
        raise TaskUsageError(
            "fence_identity_conflict",
            {
                "resolved_run_id": run_id,
                "resolved_task_id": task_id,
                "fence_run_id": fence["run_id"],
                "fence_task_id": fence["task_id"],
            },
        )
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        try:
            _check_schema(conn)
            session_id = fence["session_id"]
            rows = conn.execute(ASSISTANT_SELECT, (session_id,)).fetchall()
            user_count = conn.execute(USER_COUNT_SELECT, (session_id,)).fetchone()[0]
            total_changes = conn.total_changes
            if request_ids:
                selected, duplicates = _resolve_request_ids(
                    conn, session_id, request_ids
                )
                attribution = "operator_selected_requests"
            else:
                selected, duplicates = [], []
                attribution = "session_only"
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()

    aggregate = _aggregate(rows)
    payload: dict[str, Any] = {
        "schema": USAGE_SCHEMA,
        "ok": True,
        "read_only": {
            "uri_mode": "ro",
            "query_only": True,
            "db_total_changes": total_changes,
        },
        "storage_scope": STORAGE_SCOPE,
        "identity": {
            "db": str(Path(db_path)),
            "task_dir": str(task_dir),
            "attempt_id": attempt_id,
            "attempt_match": True,
            "run_id": run_id,
            "task_id": task_id,
            "fence": {
                key: fence[key]
                for key in ("session_id", "task_id", "worker_id", "instance_id", "run_id")
            },
        },
        "attribution": attribution,
        "attribution_note": (
            "session-scoped raw counts; not a task total and not "
            "protocol-authenticated task attribution"
            if attribution == "session_only"
            else (
                "operator-selected request IDs prove in-session relation only; "
                "not protocol-authenticated task attribution and not a "
                "billing-grade task total"
            )
        ),
        "requested_request_ids": list(request_ids),
        "unique_request_ids": selected,
        "duplicate_request_ids_ignored": duplicates,
        "session_summary": {
            "user_messages": user_count,
            "assistant_messages": len(rows),
            "assistant_set_empty": len(rows) == 0,
            "tokens": _tokens_report(len(rows), aggregate["session_buckets"]),
            "error_nonnull_assistant_messages": aggregate["error_rows"],
            "models": aggregate["models"],
            "time": _time_report(aggregate["created"], aggregate["completed"]),
        },
        "selection": [],
        "child_usage": "not_collected",
        "direct_child_count": "not_collected",
        "actual_cost": "unknown",
        "coverage_notes": [
            "record coverage does not imply provider billing coverage",
            "stored cost is not actual cost; actual cost is unknown",
            "error and canceled records are included, not discarded",
            "child/subagent usage is not collected in this query",
        ],
    }
    if len(rows) == 0:
        payload["empty_session_note"] = (
            "session has no assistant messages; usage is unknown, not measured zero"
        )
    if request_ids:
        selection = [
            _selection_entry(rid, aggregate["per_parent"].get(rid)) for rid in selected
        ]
        payload["selection"] = selection
        payload["selected_assistant_messages"] = sum(
            item["assistant_messages"] for item in selection
        )
        if payload["selected_assistant_messages"] == 0:
            payload["empty_selection_note"] = (
                "no assistant messages reference the selected requests; "
                "this is not measured zero usage"
            )
    return payload
