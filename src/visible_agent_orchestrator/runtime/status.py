"""Read-only RuntimeAdapter status summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backends import default_backend_name, get_backend, is_backend_live_enabled
from .errors import RuntimeAdapterError
from .health import utc_now
from .tmux_capabilities import command_support_report


def build_worker_status_summary(
    root: Path,
    worker_id: str | None = None,
    backend: str = "tmux-reference",
    fixture_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    fixture = Path(fixture_dir).resolve() if fixture_dir else None
    if backend == "rmux-spike" and fixture is None:
        raise RuntimeAdapterError("rmux-spike fixture required; live rmux disabled in spike-1")
    adapter = get_backend(backend, fixture_dir=fixture)
    worker = worker_id or ""
    if not worker:
        discovered = adapter.discover_workers(root)
        if not discovered:
            raise RuntimeAdapterError("no workers discovered")
        worker = discovered[0].worker_id
    status = adapter.observe_status(root, worker)
    verify = adapter.verify_binding(root, worker)
    metadata = adapter.collect_runtime_metadata(root, worker)
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(root),
        "worker_id": worker,
        "runtime_backend": backend,
        "backend_type": "rmux" if backend.startswith("rmux") else "tmux",
        "default_backend": default_backend_name(),
        "rmux_default": str(default_backend_name()).startswith("rmux"),
        "live_enabled": is_backend_live_enabled(backend),
        "fixture_mode": bool(fixture),
        **command_support_report(),
        "ok": bool(verify.ok),
        "status": status,
        "substrate_status": status,
        "verify": verify.to_dict(),
        "metadata": metadata,
        "telemetry_only": True,
    }


def build_all_workers_status_summary(
    root: Path,
    worker_id: str | None = None,
    backend: str = "tmux-reference",
    fixture_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    fixture = Path(fixture_dir).resolve() if fixture_dir else None
    if backend == "rmux-spike" and fixture is None:
        raise RuntimeAdapterError("rmux-spike fixture required; live rmux disabled in spike-1")
    adapter = get_backend(backend, fixture_dir=fixture)
    ids = [worker_id] if worker_id else [worker.worker_id for worker in adapter.discover_workers(root)]
    workers = [
        build_worker_status_summary(root, str(item), backend=backend, fixture_dir=fixture)
        for item in ids
        if item
    ]
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(root),
        "runtime_backend": backend,
        "rmux_default": str(default_backend_name()).startswith("rmux"),
        **command_support_report(),
        "workers": workers,
        "ok": all(item.get("ok", False) for item in workers),
        "telemetry_only": True,
    }
