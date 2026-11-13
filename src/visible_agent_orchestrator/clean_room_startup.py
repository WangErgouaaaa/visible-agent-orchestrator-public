"""Fail-closed admission for strict visible Parent startup."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Mapping


PLUGIN_ID = "omx-clean-room-startup-gate"
GATE_VERSION = 1
STARTUP_MODE = "exact_parent_read"
MAX_PARENT_BYTES = 50 * 1024
MAX_PARENT_LINES = 2_000
MAX_PARENT_LINE_LENGTH = 2_000


class CleanRoomStartupError(ValueError):
    """Raised before delivery when the strict startup gate is not proven."""


def gate_evidence_path(root: Path, opencode_pid: int) -> Path:
    return (
        root
        / ".omx"
        / "framework"
        / "team"
        / f"opencode_clean_room_startup_gate_{int(opencode_pid)}.json"
    )


def session_binding_path(root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return (
        root
        / ".omx"
        / "framework"
        / "team"
        / "visible-runs"
        / "_clean-room-startup-sessions"
        / f"{digest}.json"
    )


def load_task_contract(path: Path) -> dict[str, Any] | None:
    strict_hint = _read_json(path.with_name("dispatch.json")).get(
        "clean_room_startup_gate"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(strict_hint, Mapping):
            raise CleanRoomStartupError("task_metadata_invalid") from exc
        return None
    if not isinstance(value, dict):
        if isinstance(strict_hint, Mapping):
            raise CleanRoomStartupError("task_metadata_invalid")
        return None
    contract = value.get("task_contract")
    if contract is None:
        if isinstance(strict_hint, Mapping):
            raise CleanRoomStartupError("task_metadata_invalid")
        return None
    if not isinstance(contract, dict):
        if isinstance(strict_hint, Mapping):
            raise CleanRoomStartupError("task_metadata_invalid")
        return None
    if isinstance(strict_hint, Mapping):
        startup = contract.get("clean_room_startup")
        if (
            not isinstance(startup, Mapping)
            or any(
                startup.get(key) != strict_hint.get(key)
                for key in ("mode", "parent_path", "parent_sha256")
            )
        ):
            raise CleanRoomStartupError("task_metadata_invalid")
    return contract


def validate_contract(
    root: Path,
    task_contract: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    contract = task_contract if isinstance(task_contract, Mapping) else {}
    raw = contract.get("clean_room_startup")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise CleanRoomStartupError("clean_room_startup must be an object")
    expected_keys = {"mode", "parent_path", "parent_sha256"}
    unknown = sorted(set(raw) - expected_keys)
    if unknown:
        raise CleanRoomStartupError(
            "clean_room_startup has unknown field(s): "
            + ", ".join(unknown)
        )
    mode = str(raw.get("mode") or "").strip()
    parent_text = str(raw.get("parent_path") or "").strip()
    parent_sha256 = str(raw.get("parent_sha256") or "").strip()
    if mode != STARTUP_MODE:
        raise CleanRoomStartupError(
            f"clean_room_startup.mode must be {STARTUP_MODE}"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", parent_sha256):
        raise CleanRoomStartupError(
            "clean_room_startup.parent_sha256 must be lowercase SHA-256"
        )
    parent = Path(parent_text)
    if not parent.is_absolute() or ".." in parent.parts:
        raise CleanRoomStartupError(
            "clean_room_startup.parent_path must be an absolute canonical path"
        )
    try:
        resolved = parent.resolve(strict=True)
        metadata = parent.lstat()
    except OSError as exc:
        raise CleanRoomStartupError(
            "clean_room_startup.parent_path is unavailable"
        ) from exc
    if resolved != parent or not stat.S_ISREG(metadata.st_mode):
        raise CleanRoomStartupError(
            "clean_room_startup.parent_path must be a canonical regular file"
        )
    visible_runs = (
        root.resolve()
        / ".omx"
        / "framework"
        / "team"
        / "visible-runs"
    )
    if not parent.is_relative_to(visible_runs):
        raise CleanRoomStartupError(
            "clean_room_startup.parent_path must be under visible-runs"
        )
    parent_bytes = parent.read_bytes()
    try:
        parent_text_value = parent_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CleanRoomStartupError(
            "clean_room_startup.parent_path must be UTF-8 text"
        ) from exc
    lines = parent_text_value.splitlines()
    if (
        len(parent_bytes) > MAX_PARENT_BYTES
        or len(lines) > MAX_PARENT_LINES
        or any(
            len(line.encode("utf-8")) > MAX_PARENT_LINE_LENGTH
            for line in lines
        )
    ):
        raise CleanRoomStartupError(
            "clean_room_startup.parent_path exceeds OpenCode full-read limits"
        )
    actual_sha256 = hashlib.sha256(parent_bytes).hexdigest()
    if actual_sha256 != parent_sha256:
        raise CleanRoomStartupError(
            "clean_room_startup.parent_sha256 does not match parent_path"
        )
    return {
        "mode": mode,
        "parent_path": str(parent),
        "parent_sha256": parent_sha256,
    }


def arm_session(
    root: Path,
    task_dir: Path,
    worker_id: str,
    session_id: str,
    task_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    startup = validate_contract(root, task_contract)
    if startup is None:
        return {}
    if not session_id:
        raise CleanRoomStartupError("clean_room_startup_session_id_missing")
    visible_runs = (
        root.resolve()
        / ".omx"
        / "framework"
        / "team"
        / "visible-runs"
    )
    resolved_task = task_dir.resolve()
    if not resolved_task.is_relative_to(visible_runs):
        raise CleanRoomStartupError("clean_room_startup_task_path_invalid")
    binding = {
        "schema_version": 1,
        "evidence_kind": "clean_room_startup_session_binding",
        "status": "armed",
        "session_id": session_id,
        "worker_id": worker_id,
        "task_path": str(resolved_task.relative_to(root.resolve())),
        **startup,
    }
    armed = {
        "schema_version": 1,
        "evidence_kind": "clean_room_startup_gate_armed",
        "status": "armed",
        "session_id": session_id,
        "worker_id": worker_id,
        "task_path": binding["task_path"],
    }
    _write_json_atomic(resolved_task / "startup_gate_armed.json", armed)
    _write_json_atomic(session_binding_path(root, session_id), binding)
    return binding


def require_gate(
    root: Path,
    worker: Mapping[str, Any],
    task_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    startup = validate_contract(root, task_contract)
    if startup is None:
        return {}
    opencode_pid = int(worker.get("opencode_pid") or 0)
    start_ticks = int(worker.get("opencode_start_time_ticks") or 0)
    evidence_path = gate_evidence_path(root, opencode_pid)
    evidence = _read_json(evidence_path)
    hooks = evidence.get("hooks")
    valid = (
        opencode_pid > 0
        and start_ticks > 0
        and evidence.get("plugin_id") == PLUGIN_ID
        and evidence.get("gate_version") == GATE_VERSION
        and evidence.get("loaded") is True
        and str(evidence.get("control_root") or "") == str(root.resolve())
        and evidence.get("opencode_pid") == opencode_pid
        and evidence.get("opencode_start_time_ticks") == start_ticks
        and isinstance(hooks, list)
        and all(isinstance(hook, str) for hook in hooks)
        and set(hooks)
        >= {"tool.execute.before", "tool.execute.after"}
    )
    if not valid:
        try:
            evidence_label = str(evidence_path.resolve().relative_to(root.resolve()))
        except ValueError:
            evidence_label = str(evidence_path)
        raise CleanRoomStartupError(
            "clean_room_startup_gate_not_loaded: "
            f"worker={worker.get('worker_id') or '-'} "
            f"evidence={evidence_label}"
        )
    return {
        "status": "passed",
        "evidence_path": str(evidence_path.resolve().relative_to(root.resolve())),
        "plugin_id": PLUGIN_ID,
        "gate_version": GATE_VERSION,
        **startup,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
