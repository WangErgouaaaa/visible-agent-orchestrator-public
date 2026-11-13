"""Graph-level worker-pool lease helpers for visible multi-worker runs."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60


class WorkerPoolLeaseConflict(Exception):
    def __init__(self, conflicts: list[dict[str, Any]]):
        self.conflicts = conflicts
        super().__init__("worker pool lease conflict")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def lease_root(root: Path) -> Path:
    return root / ".omx" / "framework" / "team" / "langgraph-control-plane" / "leases"


def lease_archive_root(root: Path) -> Path:
    return lease_root(root) / "archive"


def _lock_path(root: Path) -> Path:
    return lease_root(root) / ".admin.lock"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def _admin_lock(root: Path):
    lock_file = _lock_path(root)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_stale(payload: dict[str, Any], *, max_age_seconds: int) -> bool:
    holder_pid = int(payload.get("holder_pid", 0) or 0)
    created_at = _parse_utc(str(payload.get("created_at") or ""))
    if holder_pid and not _pid_alive(holder_pid):
        return True
    if created_at is None:
        return False
    age = datetime.now(timezone.utc) - created_at
    return age.total_seconds() > max_age_seconds


def _active_lease_files(root: Path) -> list[Path]:
    base = lease_root(root)
    if not base.is_dir():
        return []
    return sorted(
        path
        for path in base.glob("*.json")
        if path.is_file() and path.parent == base
    )


def cleanup_stale_worker_pool_leases(root: Path, *, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> list[dict[str, Any]]:
    root = Path(root).resolve()
    cleaned: list[dict[str, Any]] = []
    with _admin_lock(root):
        for path in _active_lease_files(root):
            payload = _read_json(path)
            if not payload:
                continue
            if not _is_stale(payload, max_age_seconds=max_age_seconds):
                continue
            payload["status"] = "stale_released"
            payload["released_at"] = utc_now()
            payload["release_reason"] = "stale_owner_or_expired"
            archive = lease_archive_root(root) / f"{path.stem}.{payload['released_at'].replace(':', '').replace('-', '')}.json"
            _write_json(archive, payload)
            path.unlink(missing_ok=True)
            cleaned.append(payload)
    return cleaned


def acquire_worker_pool_lease(
    root: Path,
    workers: list[str],
    *,
    title: str,
    thread_id: str,
    holder_kind: str = "langgraph_control_plane",
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    root = Path(root).resolve()
    unique_workers = sorted({str(item).strip() for item in workers if str(item).strip()})
    if not unique_workers:
        raise ValueError("at least one worker is required for lease acquisition")
    with _admin_lock(root):
        cleaned: list[dict[str, Any]] = []
        for path in _active_lease_files(root):
            payload = _read_json(path)
            if not payload:
                continue
            if _is_stale(payload, max_age_seconds=max_age_seconds):
                payload["status"] = "stale_released"
                payload["released_at"] = utc_now()
                payload["release_reason"] = "stale_owner_or_expired"
                archive = lease_archive_root(root) / f"{path.stem}.{payload['released_at'].replace(':', '').replace('-', '')}.json"
                _write_json(archive, payload)
                path.unlink(missing_ok=True)
                cleaned.append(payload)
        conflicts: list[dict[str, Any]] = []
        requested = set(unique_workers)
        for path in _active_lease_files(root):
            payload = _read_json(path)
            if not payload:
                continue
            active_workers = {str(item) for item in payload.get("workers", []) if str(item)}
            overlap = sorted(requested.intersection(active_workers))
            if overlap:
                conflicts.append(
                    {
                        "lease_id": str(payload.get("lease_id") or path.stem),
                        "thread_id": str(payload.get("thread_id") or ""),
                        "title": str(payload.get("title") or ""),
                        "workers": sorted(active_workers),
                        "overlap_workers": overlap,
                        "holder_pid": int(payload.get("holder_pid", 0) or 0),
                        "created_at": str(payload.get("created_at") or ""),
                    }
                )
        if conflicts:
            raise WorkerPoolLeaseConflict(conflicts)
        lease_id = f"wlease-{uuid.uuid4().hex[:12]}"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "lease_id": lease_id,
            "root": str(root),
            "thread_id": thread_id,
            "title": title,
            "workers": unique_workers,
            "holder_kind": holder_kind,
            "holder_pid": os.getpid(),
            "created_at": utc_now(),
            "status": "active",
            "cleaned_stale_count": len(cleaned),
        }
        _write_json(lease_root(root) / f"{lease_id}.json", payload)
        return payload


def release_worker_pool_lease(root: Path, lease_id: str, *, reason: str = "completed") -> dict[str, Any]:
    root = Path(root).resolve()
    with _admin_lock(root):
        path = lease_root(root) / f"{lease_id}.json"
        payload = _read_json(path)
        if not payload:
            return {}
        payload["status"] = "released"
        payload["released_at"] = utc_now()
        payload["release_reason"] = reason
        archive = lease_archive_root(root) / f"{lease_id}.{payload['released_at'].replace(':', '').replace('-', '')}.json"
        _write_json(archive, payload)
        path.unlink(missing_ok=True)
        return payload


def active_worker_pool_leases_summary(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    cleanup_stale_worker_pool_leases(root)
    leases: list[dict[str, Any]] = []
    for path in _active_lease_files(root):
        payload = _read_json(path)
        if not payload:
            continue
        leases.append(
            {
                "lease_id": str(payload.get("lease_id") or path.stem),
                "thread_id": str(payload.get("thread_id") or ""),
                "title": str(payload.get("title") or ""),
                "workers": [str(item) for item in payload.get("workers", []) if str(item)],
                "holder_pid": int(payload.get("holder_pid", 0) or 0),
                "created_at": str(payload.get("created_at") or ""),
            }
        )
    return {
        "count": len(leases),
        "leases": leases,
        "workers": sorted({worker for lease in leases for worker in lease.get("workers", [])}),
    }
