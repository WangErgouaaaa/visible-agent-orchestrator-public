from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visible_agent_orchestrator.worker_pool_lease import (
    WorkerPoolLeaseConflict,
    acquire_worker_pool_lease,
    active_worker_pool_leases_summary,
    cleanup_stale_worker_pool_leases,
    lease_root,
    release_worker_pool_lease,
)


def test_worker_pool_lease_acquire_and_release(tmp_path: Path) -> None:
    payload = acquire_worker_pool_lease(
        tmp_path,
        ["vw-worker-1", "vw-worker-2"],
        title="test lease",
        thread_id="thread-test",
    )
    summary = active_worker_pool_leases_summary(tmp_path)

    assert summary["count"] == 1
    assert summary["workers"] == ["vw-worker-1", "vw-worker-2"]
    assert summary["leases"][0]["thread_id"] == "thread-test"

    released = release_worker_pool_lease(tmp_path, payload["lease_id"], reason="completed")
    assert released["release_reason"] == "completed"
    assert active_worker_pool_leases_summary(tmp_path)["count"] == 0


def test_worker_pool_lease_conflict_reports_overlap(tmp_path: Path) -> None:
    first = acquire_worker_pool_lease(
        tmp_path,
        ["vw-worker-1", "vw-worker-2"],
        title="first lease",
        thread_id="thread-first",
    )
    try:
        try:
            acquire_worker_pool_lease(
                tmp_path,
                ["vw-worker-2", "vw-worker-3"],
                title="second lease",
                thread_id="thread-second",
            )
            assert False, "expected WorkerPoolLeaseConflict"
        except WorkerPoolLeaseConflict as exc:
            assert exc.conflicts[0]["thread_id"] == "thread-first"
            assert exc.conflicts[0]["overlap_workers"] == ["vw-worker-2"]
    finally:
        release_worker_pool_lease(tmp_path, first["lease_id"], reason="cleanup")


def test_worker_pool_lease_cleanup_releases_stale_dead_pid(tmp_path: Path) -> None:
    base = lease_root(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    stale_path = base / "wlease-stale.json"
    stale_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "lease_id": "wlease-stale",
                "thread_id": "thread-stale",
                "title": "stale",
                "workers": ["vw-worker-1"],
                "holder_pid": 999999,
                "created_at": "2026-01-01T00:00:00Z",
                "status": "active",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    cleaned = cleanup_stale_worker_pool_leases(tmp_path, max_age_seconds=1)

    assert [item["lease_id"] for item in cleaned] == ["wlease-stale"]
    assert active_worker_pool_leases_summary(tmp_path)["count"] == 0
