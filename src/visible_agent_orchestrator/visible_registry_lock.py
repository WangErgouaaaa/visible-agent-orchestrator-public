"""Shared serialization for visible-worker registry mutations."""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


INACTIVE_STALE_VISIBLE_WORKER_STATUSES = {
    "disabled",
    "retired",
    "stale_binding_disabled",
}


def registry_lock_path_for_registry(registry_path: Path) -> Path:
    return Path(registry_path).resolve().parent / "rebind" / ".registry-rebind.lock"


@contextmanager
def visible_registry_path_lock(registry_path: Path) -> Iterator[None]:
    path = registry_lock_path_for_registry(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def visible_registry_lock(root: Path) -> Iterator[None]:
    registry_path = (
        Path(root).resolve()
        / ".omx"
        / "framework"
        / "team"
        / "opencode_visible_workers.json"
    )
    with visible_registry_path_lock(registry_path):
        yield


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def owner_lock_identity(data: dict[str, Any]) -> tuple[str, ...]:
    owner_lock_id = str(data.get("owner_lock_id") or "")
    if owner_lock_id:
        return ("owner_lock_id", owner_lock_id)
    return (
        "legacy",
        str(data.get("worker_id") or ""),
        str(data.get("run") or ""),
        str(data.get("task") or ""),
        str(data.get("created_at") or ""),
        str(data.get("binding_generation") or ""),
        str(data.get("binding_lease_id") or ""),
    )


def owner_lock_version(data: dict[str, Any]) -> int:
    try:
        return max(0, int(data.get("owner_lock_version") or 0))
    except (TypeError, ValueError):
        return 0


def owner_lock_transition_fence(data: dict[str, Any]) -> tuple[str, ...]:
    return (
        *owner_lock_identity(data),
        str(owner_lock_version(data)),
        str(data.get("status") or ""),
    )


def _normalized_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalized_pid_set(value: Any) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {
        pid
        for item in value
        if (pid := _normalized_int(item)) > 0
    }


def _normalized_host(value: Any) -> str:
    host = str(value or "127.0.0.1").strip().lower().strip("[]")
    if host == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return "loopback" if address.is_loopback else address.compressed


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def is_inactive_stale_visible_worker(record: dict[str, Any]) -> bool:
    status = str(record.get("status") or "").strip()
    return bool(
        status in INACTIVE_STALE_VISIBLE_WORKER_STATUSES
        and not _coerce_bool(record.get("allow_auto_submit"))
        and not _coerce_bool(record.get("default_for_new_tasks"))
    )


def _has_active_live_evidence(record: dict[str, Any]) -> bool:
    return any(
        _coerce_bool(record.get(key))
        for key in ("server_live", "doc_available")
    )


def is_cross_boot_pane_reuse(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    shared_aliases: list[str],
) -> bool:
    existing_boot = str(existing.get("host_boot_id") or "").strip()
    incoming_boot = str(incoming.get("host_boot_id") or "").strip()
    # ponytail: registry-only liveness gate; live old-port probing belongs to the caller.
    return bool(
        existing_boot
        and incoming_boot
        and existing_boot != incoming_boot
        and _normalized_host(existing.get("host"))
        == _normalized_host(incoming.get("host"))
        and any(alias.startswith("pane_id=") for alias in shared_aliases)
        and not _has_active_live_evidence(existing)
    )


def is_cross_tmux_server_pane_reuse(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    shared_aliases: list[str],
) -> bool:
    existing_boot = str(existing.get("host_boot_id") or "").strip()
    incoming_boot = str(incoming.get("host_boot_id") or "").strip()
    existing_server = _normalized_int(existing.get("tmux_server_pid"))
    incoming_server = _normalized_int(incoming.get("tmux_server_pid"))
    return bool(
        existing_boot
        and incoming_boot
        and existing_boot == incoming_boot
        and existing_server > 0
        and incoming_server > 0
        and existing_server != incoming_server
        and _normalized_host(existing.get("host"))
        == _normalized_host(incoming.get("host"))
        and len(shared_aliases) == 1
        and shared_aliases[0].startswith("pane_id=")
        and not _has_active_live_evidence(existing)
    )


def visible_binding_alias_conflicts(
    workers: list[Any],
    incoming: dict[str, Any],
    *,
    ignore_worker_id: str = "",
    ignore_inactive_stale_aliases: bool = False,
    ignored_inactive_aliases: list[str] | None = None,
    ignore_cross_boot_pane_reuse: bool = False,
    ignored_cross_boot_aliases: list[str] | None = None,
    ignore_cross_tmux_server_pane_reuse: bool = False,
    ignored_cross_tmux_server_aliases: list[str] | None = None,
    conflicting_worker_ids: list[str] | None = None,
) -> list[str]:
    """Return physical binding collisions with other visible worker IDs."""

    incoming_worker = str(incoming.get("worker_id") or "")
    ignored = ignore_worker_id or incoming_worker
    incoming_pane = str(incoming.get("pane_id") or "")
    incoming_host_display = str(incoming.get("host") or "127.0.0.1")
    incoming_host = _normalized_host(incoming_host_display)
    incoming_port = _normalized_int(incoming.get("port"))
    incoming_pane_pid = _normalized_int(incoming.get("pane_pid"))
    incoming_listener_pids = _normalized_pid_set(incoming.get("listener_pids"))
    incoming_opencode_pids = _normalized_pid_set(incoming.get("opencode_pids"))

    conflicts: list[str] = []
    for item in workers:
        if not isinstance(item, dict):
            continue
        worker_id = str(item.get("worker_id") or "")
        if not worker_id or worker_id == ignored:
            continue

        shared: list[str] = []
        pane_id = str(item.get("pane_id") or "")
        if incoming_pane and pane_id == incoming_pane:
            shared.append(f"pane_id={incoming_pane}")

        host = _normalized_host(item.get("host"))
        port = _normalized_int(item.get("port"))
        if incoming_port > 0 and port == incoming_port and host == incoming_host:
            shared.append(
                f"host_port={incoming_host_display}:{incoming_port}"
            )

        pane_pid = _normalized_int(item.get("pane_pid"))
        if incoming_pane_pid > 0 and pane_pid == incoming_pane_pid:
            shared.append(f"pane_pid={incoming_pane_pid}")

        listener_overlap = sorted(
            incoming_listener_pids
            & _normalized_pid_set(item.get("listener_pids"))
        )
        if listener_overlap:
            shared.append(
                "listener_pids=" + ",".join(str(pid) for pid in listener_overlap)
            )

        opencode_overlap = sorted(
            incoming_opencode_pids
            & _normalized_pid_set(item.get("opencode_pids"))
        )
        if opencode_overlap:
            shared.append(
                "opencode_pids=" + ",".join(str(pid) for pid in opencode_overlap)
            )

        if (
            shared
            and ignore_cross_boot_pane_reuse
            and is_cross_boot_pane_reuse(item, incoming, shared)
        ):
            if ignored_cross_boot_aliases is not None:
                ignored_cross_boot_aliases.append(worker_id)
            continue

        if (
            shared
            and ignore_cross_tmux_server_pane_reuse
            and is_cross_tmux_server_pane_reuse(item, incoming, shared)
        ):
            if ignored_cross_tmux_server_aliases is not None:
                ignored_cross_tmux_server_aliases.append(worker_id)
            continue

        if (
            shared
            and ignore_inactive_stale_aliases
            and is_inactive_stale_visible_worker(item)
        ):
            if ignored_inactive_aliases is not None:
                ignored_inactive_aliases.append(worker_id)
            continue

        if shared:
            conflicts.append(
                f"worker {worker_id!r} shares " + ", ".join(shared)
            )
            if conflicting_worker_ids is not None:
                conflicting_worker_ids.append(worker_id)
    return conflicts


def write_visible_registry_mutation_audit(
    registry_path: Path,
    *,
    worker_id: str,
    action: str,
    old_record: dict[str, Any],
    new_record: dict[str, Any],
    cas_result: str = "locked_plan_revalidated",
    reason: str = "authorized visible registry mutation applied",
    result: str = "applied",
    transaction_id: str = "",
    audit_path: Path | None = None,
    created_at: str = "",
) -> Path:
    created_at = created_at or _utc_now()
    payload = {
        "schema_version": 1,
        "audit_kind": "visible_worker_binding_mutation",
        "action": action,
        "worker_id": worker_id,
        "result": result,
        "cas_result": cas_result,
        "reason": reason,
        "transaction_id": transaction_id,
        "old_record": old_record,
        "new_record": new_record,
        "caller": {
            "argv": list(sys.argv),
            "cwd": os.getcwd(),
            "TMUX": str(os.environ.get("TMUX") or ""),
            "TMUX_PANE": str(os.environ.get("TMUX_PANE") or ""),
            "tmux_session": str(
                os.environ.get("OMX_TMUX_SESSION")
                or new_record.get("tmux_session")
                or old_record.get("tmux_session")
                or ""
            ),
            "tmux_window": str(
                os.environ.get("OMX_TMUX_WINDOW")
                or new_record.get("tmux_window")
                or old_record.get("tmux_window")
                or ""
            ),
        },
        "created_at": created_at,
        "updated_at": _utc_now(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    safe_worker = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in worker_id
    )
    timestamp = "".join(char for char in created_at if char.isalnum())
    path = audit_path
    if path is None:
        path = (
            Path(registry_path).resolve().parent
            / "visible-binding-audit"
            / (safe_worker or "worker")
            / f"{timestamp}-{time.time_ns()}-{digest}.json"
        )
    write_json_atomic(path, payload)
    return path
