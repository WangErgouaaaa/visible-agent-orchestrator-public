from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.terminal_acceptance import (
    terminal_acceptance_status,
)

PROGRESS_CHECKPOINTS_FILENAME = "progress_checkpoints.jsonl"
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_TEXT_CHAR_LIMIT = 512
CHECKPOINT_EVIDENCE_PATH_LIMIT = 8
CHECKPOINT_EVIDENCE_PATH_CHAR_LIMIT = 512
CHECKPOINT_RECORD_BYTE_LIMIT = 4096
CHECKPOINT_RECORD_LIMIT = 512
CHECKPOINT_QUERY_RETURN_LIMIT = 20
CHECKPOINT_STORE_MAX_BYTES = CHECKPOINT_RECORD_LIMIT * (CHECKPOINT_RECORD_BYTE_LIMIT + 8)

_TEXT_FIELDS = ("name", "summary", "next_action", "blocker")


def validate_checkpoint_content(
    *,
    name: str,
    summary: str,
    next_action: str,
    blocker: str,
    evidence_paths: list[str],
) -> list[str]:
    errors: list[str] = []
    if not summary.strip():
        errors.append("summary is required")
    for field in _TEXT_FIELDS:
        if len(locals()[field]) > CHECKPOINT_TEXT_CHAR_LIMIT:
            errors.append(
                f"{field} exceeds {CHECKPOINT_TEXT_CHAR_LIMIT} characters"
            )
    if len(evidence_paths) > CHECKPOINT_EVIDENCE_PATH_LIMIT:
        errors.append(
            f"evidence_paths exceeds {CHECKPOINT_EVIDENCE_PATH_LIMIT} entries"
        )
    for item in evidence_paths:
        shape_error = _evidence_shape_error(item)
        if shape_error:
            errors.append(shape_error)
            break
    return errors


def _evidence_shape_error(item: str) -> str | None:
    if not item or len(item) > CHECKPOINT_EVIDENCE_PATH_CHAR_LIMIT:
        return "evidence_paths entries must be 1..512 characters"
    if "\x00" in item or item.startswith("/") or "\\" in item:
        return "evidence_paths entries must be relative paths"
    if any(part == ".." for part in Path(item).parts):
        return "evidence_paths entries must not contain '..'"
    return None


def evidence_path_in_scope(task_dir: Path, item: str) -> bool:
    """Evidence references must resolve inside the owning task directory."""
    if _evidence_shape_error(item):
        return False
    base = os.path.realpath(task_dir)
    resolved = os.path.realpath(task_dir / item)
    return resolved == base or resolved.startswith(base + os.sep)


def canonical_request_hash(
    *,
    request_id: str,
    worker: str,
    attempt_id: str,
    name: str,
    summary: str,
    next_action: str,
    blocker: str,
    evidence_paths: list[str],
    needs_decision: bool,
    key: bool,
) -> str:
    payload = json.dumps(
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "request_id": request_id,
            "worker": worker,
            "attempt_id": attempt_id,
            "name": name,
            "summary": summary,
            "next_action": next_action,
            "blocker": blocker,
            "evidence_paths": list(evidence_paths),
            "needs_decision": bool(needs_decision),
            "key": bool(key),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checkpoint_record(
    *,
    ts: str,
    request_id: str,
    worker: str,
    attempt_id: str,
    seq: int,
    name: str,
    summary: str,
    next_action: str,
    blocker: str,
    evidence_paths: list[str],
    needs_decision: bool,
    key: bool,
    request_hash: str,
) -> dict[str, Any]:
    record = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "ts": ts,
        "request_id": request_id,
        "worker": worker,
        "attempt_id": attempt_id,
        "seq": seq,
        "name": name,
        "summary": summary,
        "evidence_paths": list(evidence_paths),
        "next_action": next_action,
        "needs_decision": bool(needs_decision),
        "blocker": blocker or None,
        "key": bool(key),
        "request_hash": request_hash,
    }
    encoded = len(
        json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    if encoded > CHECKPOINT_RECORD_BYTE_LIMIT:
        raise ValueError(
            f"checkpoint record exceeds {CHECKPOINT_RECORD_BYTE_LIMIT} bytes"
        )
    return record


def _valid_record(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return False
    if type(value.get("seq")) is not int:
        return False
    for field in (
        "ts",
        "request_id",
        "worker",
        "attempt_id",
        "name",
        "summary",
        "next_action",
    ):
        if not isinstance(value.get(field), str):
            return False
    if not value.get("request_id") or not value.get("worker") or not value.get("attempt_id"):
        return False
    if not isinstance(value.get("request_hash"), str) or not value["request_hash"]:
        return False
    if not isinstance(value.get("needs_decision"), bool):
        return False
    if not isinstance(value.get("key"), bool):
        return False
    blocker = value.get("blocker")
    if blocker is not None and not isinstance(blocker, str):
        return False
    evidence = value.get("evidence_paths")
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) for item in evidence
    ):
        return False
    return not validate_checkpoint_content(
        name=value["name"],
        summary=value["summary"],
        next_action=value["next_action"],
        blocker=blocker or "",
        evidence_paths=evidence,
    )


def _record_evidence_in_scope(record: dict[str, Any], task_dir: Path) -> bool:
    return all(
        evidence_path_in_scope(task_dir, item)
        for item in record.get("evidence_paths") or []
    )


def read_progress_checkpoints(task_dir: Path) -> dict[str, Any]:
    """Read one task's checkpoint store with bounded, validated parsing.

    Enforces size and count bounds before reading; malformed records are
    diagnostics and never valid current evidence. The store is authoritative
    only when every integrity flag is clean.
    """
    path = task_dir / PROGRESS_CHECKPOINTS_FILENAME
    records: list[dict[str, Any]] = []
    invalid_count = 0
    incomplete_tail = False
    unsafe_evidence_count = 0
    oversize_store = False
    if path.is_file():
        size = path.stat().st_size
        if size > CHECKPOINT_STORE_MAX_BYTES:
            oversize_store = True
        else:
            raw = path.read_bytes()
            missing_final_newline = bool(raw) and not raw.endswith(b"\n")
            lines = raw.decode("utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    value = None
                if not _valid_record(value):
                    invalid_count += 1
                    if index == len(lines) - 1:
                        incomplete_tail = True
                    continue
                if not _record_evidence_in_scope(value, task_dir):
                    unsafe_evidence_count += 1
                    continue
                records.append(value)
            if missing_final_newline:
                incomplete_tail = True
    nondecreasing = all(
        later.get("seq", 0) > earlier.get("seq", 0)
        for earlier, later in zip(records, records[1:])
    )
    unique_seqs = len({int(record.get("seq", 0)) for record in records})
    sequence_conflict = unique_seqs != len(records) or not nondecreasing
    return {
        "records": records,
        "invalid_count": invalid_count,
        "unsafe_evidence_count": unsafe_evidence_count,
        "incomplete_tail": incomplete_tail,
        "oversize_store": oversize_store,
        "out_of_order": not nondecreasing,
        "sequence_conflict": sequence_conflict,
        "limit_reached": len(records) >= CHECKPOINT_RECORD_LIMIT,
        "append_blocked": bool(
            oversize_store
            or sequence_conflict
            or incomplete_tail
            or len(records) >= CHECKPOINT_RECORD_LIMIT
        ),
    }


def store_integrity_errors(store: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if store.get("oversize_store"):
        errors.append(
            f"store exceeds {CHECKPOINT_STORE_MAX_BYTES} bytes; refusing append"
        )
    if store.get("incomplete_tail"):
        errors.append("store has damaged non-newline tail; refusing append")
    if store.get("invalid_count"):
        errors.append(
            f"store has {store['invalid_count']} malformed records; refusing append"
        )
    if store.get("sequence_conflict"):
        errors.append("store has conflicting or non-increasing seq; refusing append")
    if store.get("limit_reached"):
        errors.append(
            f"store reached {CHECKPOINT_RECORD_LIMIT} records; refusing new records"
        )
    return errors


def current_task_identity(task_dir: Path) -> dict[str, Any]:
    """Derive the authoritative current worker/attempt from control-plane files."""
    owner = ""
    owner_path = task_dir / "owner"
    if owner_path.is_file():
        owner = owner_path.read_text(encoding="utf-8", errors="replace").strip()
    metadata = _read_json_object(task_dir / "task.json")
    fence = _read_json_object(task_dir / "acceptance_fence.json")
    task_json_attempt = str(metadata.get("attempt_id") or "")
    fence_attempt = str(fence.get("attempt_id") or "")
    conflict = bool(
        task_json_attempt and fence_attempt and task_json_attempt != fence_attempt
    )
    if conflict:
        return {
            "known": False,
            "worker": owner,
            "attempt_id": "",
            "source": "",
            "conflict": True,
            "task_json_attempt": task_json_attempt,
            "fence_attempt": fence_attempt,
        }
    if fence_attempt:
        source = "acceptance_fence"
        attempt_id = fence_attempt
    elif task_json_attempt:
        source = "task.json"
        attempt_id = task_json_attempt
    else:
        source = ""
        attempt_id = ""
    return {
        "known": bool(owner and attempt_id),
        "worker": owner,
        "attempt_id": attempt_id,
        "source": source,
        "conflict": False,
        "task_json_attempt": task_json_attempt,
        "fence_attempt": fence_attempt,
    }


def identity_valid_records(
    store: dict[str, Any],
    identity: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Return records matching exact current worker/attempt plus a foreign count."""
    if not identity.get("known"):
        return [], len(store.get("records") or [])
    current_worker = str(identity.get("worker") or "")
    current_attempt = str(identity.get("attempt_id") or "")
    current: list[dict[str, Any]] = []
    foreign = 0
    for record in store.get("records") or []:
        if (
            str(record.get("worker") or "") == current_worker
            and str(record.get("attempt_id") or "") == current_attempt
        ):
            current.append(record)
        else:
            foreign += 1
    return current, foreign


def _record_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_checkpoint_access(root: Path, task_dir: Path) -> dict[str, Any]:
    """Read-side identity/acceptance guard for checkpoint evidence.

    Active tasks reuse the same fence/registry/current-owner-lock agreement as
    the write path before records are presented as current. A legally accepted
    terminal task stays readable after its owner lock has been archived. No new
    identity store or live network probe is involved.
    """
    identity = current_task_identity(task_dir)
    if identity.get("conflict"):
        return {
            "identity": identity,
            "ok": False,
            "mode": "active",
            "status": "identity_conflict",
        }
    if not identity.get("known"):
        return {
            "identity": identity,
            "ok": False,
            "mode": "active",
            "status": "identity_unknown",
        }
    if terminal_acceptance_status(task_dir).get("accepted"):
        return {
            "identity": identity,
            "ok": True,
            "mode": "accepted_terminal",
            "status": "accepted_terminal",
        }
    owner = str(identity.get("worker") or "")
    fence = _read_json_object(task_dir / "acceptance_fence.json")
    registry = _read_json_object(
        root / ".omx" / "framework" / "team" / "opencode_visible_workers.json"
    )
    worker = next(
        (
            item
            for item in registry.get("workers", [])
            if isinstance(item, dict)
            and str(item.get("worker_id") or "") == owner
        ),
        {},
    )
    binding_generation = _record_int(fence.get("binding_generation"))
    fencing_generation = _record_int(fence.get("fencing_generation"))
    expected_fencing = _record_int(
        worker.get("fencing_generation")
        if worker.get("fencing_generation") is not None
        else worker.get("binding_generation")
    )
    binding_matches = bool(
        worker
        and str(fence.get("worker_id") or "") == owner
        and str(fence.get("task_id") or "") == task_dir.name
        and str(fence.get("instance_id") or "") == str(worker.get("instance_id") or "")
        and binding_generation is not None
        and binding_generation == _record_int(worker.get("binding_generation"))
        and fencing_generation is not None
        and fencing_generation == expected_fencing
    )
    lock = _read_json_object(
        root
        / ".omx"
        / "framework"
        / "team"
        / "visible-runs"
        / "_locks"
        / f"{owner}.lock.json"
    )
    try:
        task_dir_rel = str(task_dir.resolve().relative_to(root.resolve()))
    except Exception:
        task_dir_rel = str(task_dir)
    lock_matches = bool(
        binding_matches
        and str(lock.get("owner_lock_id") or "")
        and str(lock.get("worker_id") or "") == owner
        and str(lock.get("run") or "") == task_dir.parent.parent.name
        and str(lock.get("task") or "") == task_dir.name
        and str(lock.get("task_dir") or "") == task_dir_rel
        and str(lock.get("status") or "") in {"submitting", "submitted"}
        and binding_generation == _record_int(lock.get("binding_generation"))
        and str(lock.get("pane_id") or "") == str(worker.get("pane_id") or "")
        and _record_int(lock.get("port")) == _record_int(worker.get("port"))
    )
    if lock_matches:
        status = "active_owner_lock"
    elif binding_matches:
        status = "owner_lock_mismatch"
    else:
        status = "binding_mismatch"
    return {
        "identity": identity,
        "ok": lock_matches,
        "mode": "active",
        "status": status,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def latest_seq(records: list[dict[str, Any]]) -> int:
    return int(records[-1].get("seq", 0)) if records else 0


def store_latest_seq(store: dict[str, Any]) -> int:
    return latest_seq(store.get("records") or [])


def earliest_page(
    records: list[dict[str, Any]],
    since_seq: int,
    limit: int = CHECKPOINT_QUERY_RETURN_LIMIT,
) -> list[dict[str, Any]]:
    """Earliest bounded page after since_seq; cursors advance only over returned records."""
    selected = [
        record
        for record in records
        if int(record.get("seq", 0)) > since_seq
    ]
    return selected[:limit]


def classify_notable(
    record: dict[str, Any],
    *,
    notify_names: list[str] | None = None,
) -> tuple[bool, str]:
    if record.get("needs_decision"):
        return True, "needs_decision"
    if record.get("blocker"):
        return True, "blocker"
    if record.get("key"):
        return True, "key_checkpoint"
    name = str(record.get("name") or "")
    if name and name in set(notify_names or []):
        return True, "contract_notified_checkpoint"
    return False, ""
