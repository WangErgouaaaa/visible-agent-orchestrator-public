"""Validated worker receipt acknowledgements for visible-runs tasks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ALLOWED_SOURCES = {"opencode_worker_plugin", "opencode_worker_prompt", "worker_plugin"}
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TERMINAL_MARKERS = ("done", "fail", "blocked")


class ReceiptAckError(Exception):
    """Raised when a worker/plugin receipt acknowledgement is not trustworthy."""


@dataclass(frozen=True)
class ReceiptAck:
    schema_version: int
    ack_kind: str
    source: str
    worker_id: str
    run: str
    task: str
    task_dir: str
    delivery_prompt_sha256: str
    received_at: str
    plugin: dict[str, Any]


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReceiptAckError("missing_delivery_json") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptAckError("malformed_delivery_json") from exc
    if not isinstance(data, dict):
        raise ReceiptAckError("malformed_delivery_json")
    return data


def _visible_runs_root(root: Path) -> Path:
    return root.resolve() / ".omx" / "framework" / "team" / "visible-runs"


def _locks_root(root: Path) -> Path:
    return _visible_runs_root(root) / "_locks"


def _parse_visible_task_path(root: Path, task_dir: Path) -> tuple[str, str]:
    resolved = task_dir.resolve(strict=False)
    try:
        parts = resolved.relative_to(_visible_runs_root(root)).parts
    except ValueError as exc:
        raise ReceiptAckError("unsafe_task_dir") from exc
    if len(parts) != 3 or parts[1] != "tasks" or parts[0] in {"_locks", "_watcher"}:
        raise ReceiptAckError("unsafe_task_dir")
    return parts[0], parts[2]


def _reject_unsafe_task_dir_text(value: str) -> None:
    if not value or "\x00" in value or "\\" in value:
        raise ReceiptAckError("unsafe_task_dir")
    if any(part == ".." for part in Path(value).parts):
        raise ReceiptAckError("unsafe_task_dir")


def _parse_ack(data: dict[str, Any]) -> ReceiptAck:
    try:
        schema_version = int(data.get("schema_version", -1))
    except (TypeError, ValueError) as exc:
        raise ReceiptAckError("bad_schema_version") from exc
    if schema_version != 1:
        raise ReceiptAckError("bad_schema_version")
    fields = {
        "ack_kind": str(data.get("ack_kind") or "").strip(),
        "source": str(data.get("source") or "").strip(),
        "worker_id": str(data.get("worker_id") or "").strip(),
        "run": str(data.get("run") or "").strip(),
        "task": str(data.get("task") or "").strip(),
        "task_dir": str(data.get("task_dir") or "").strip(),
        "delivery_prompt_sha256": str(data.get("delivery_prompt_sha256") or "").strip(),
        "received_at": str(data.get("received_at") or "").strip(),
    }
    if fields["ack_kind"] != "worker_receipt":
        raise ReceiptAckError("bad_ack_kind")
    if fields["source"] not in ALLOWED_SOURCES:
        raise ReceiptAckError("bad_source")
    for key in ("worker_id", "run", "task", "task_dir", "delivery_prompt_sha256", "received_at"):
        if not fields[key]:
            raise ReceiptAckError(f"missing_{key}")
    for key in ("worker_id", "run", "task"):
        if not SAFE_ID_RE.match(fields[key]):
            raise ReceiptAckError(f"bad_{key}")
    if not HEX64_RE.match(fields["delivery_prompt_sha256"]):
        raise ReceiptAckError("bad_prompt_hash")
    if not fields["received_at"].endswith("Z"):
        raise ReceiptAckError("bad_received_at")
    plugin = data.get("plugin")
    if not isinstance(plugin, dict):
        raise ReceiptAckError("bad_plugin")
    return ReceiptAck(schema_version=1, plugin=plugin, **fields)


def _read_lock_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ReceiptAckError("malformed_active_lock") from exc
    if not isinstance(data, dict):
        raise ReceiptAckError("malformed_active_lock")
    return data


def _validate_active_lock(root: Path, ack: ReceiptAck) -> None:
    lock_root = _locks_root(root)
    if not lock_root.is_dir():
        return
    worker_lock = lock_root / f"{ack.worker_id}.lock.json"
    if worker_lock.is_file():
        data = _read_lock_json(worker_lock)
        if (
            str(data.get("worker_id") or "") != ack.worker_id
            or str(data.get("run") or "") != ack.run
            or str(data.get("task") or "") != ack.task
        ):
            raise ReceiptAckError("active_lock_mismatch")
    for path in sorted(lock_root.glob("*.lock.json")):
        data = _read_lock_json(path)
        if str(data.get("run") or "") == ack.run and str(data.get("task") or "") == ack.task:
            if str(data.get("worker_id") or "") != ack.worker_id:
                raise ReceiptAckError("wrong_active_lock")


def validate_receipt_ack(root: Path, task_dir: Path, worker_id: str, data: dict[str, Any]) -> ReceiptAck:
    root = root.resolve()
    task_dir = task_dir.resolve(strict=False)
    expected_run, expected_task = _parse_visible_task_path(root, task_dir)
    ack = _parse_ack(data)
    _reject_unsafe_task_dir_text(ack.task_dir)
    ack_task_dir = (root / ack.task_dir).resolve(strict=False) if not Path(ack.task_dir).is_absolute() else Path(ack.task_dir).resolve(strict=False)
    ack_run, ack_task = _parse_visible_task_path(root, ack_task_dir)

    if ack.worker_id != worker_id:
        raise ReceiptAckError("wrong_worker")
    if ack.run != expected_run or ack.task != expected_task:
        raise ReceiptAckError("wrong_run_or_task")
    if ack_run != expected_run or ack_task != expected_task or ack_task_dir != task_dir:
        raise ReceiptAckError("wrong_task_dir")
    if not (task_dir / ".submitted").is_file():
        raise ReceiptAckError("missing_submitted_marker")
    if any((task_dir / f".{name}").is_file() for name in TERMINAL_MARKERS):
        raise ReceiptAckError("terminal_state_already_exists")
    _validate_active_lock(root, ack)

    delivery = _read_json_dict(task_dir / "delivery.json")
    expected_hash = str(delivery.get("prompt_sha256") or "").strip()
    if expected_hash and ack.delivery_prompt_sha256 != expected_hash:
        raise ReceiptAckError("wrong_prompt_hash")
    return ack
