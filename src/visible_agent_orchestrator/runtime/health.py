"""Read-only runtime health summaries for visible workers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backends import default_backend_name, get_backend, is_backend_live_enabled
from .errors import RuntimeAdapterError
from .tmux_capabilities import command_support_report


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _framework_root(root: Path) -> Path:
    return Path(root) / ".omx" / "framework" / "team"


def _visible_runs_root(root: Path) -> Path:
    return _framework_root(root) / "visible-runs"


def _watcher_root(root: Path) -> Path:
    return _visible_runs_root(root) / "_watcher"


def _locks_root(root: Path) -> Path:
    return _visible_runs_root(root) / "_locks"


def _fixture_telemetry(fixture_dir: Path | None) -> dict[str, Any]:
    if not fixture_dir:
        return {}
    return _read_json(Path(fixture_dir) / "runtime-telemetry.json")


def _live_alert_state(root: Path) -> dict[str, Any]:
    current = _read_json(_watcher_root(root) / "current_alerts.json")
    if current:
        return current
    board = _read_json(_watcher_root(root) / "visible_board.json")
    alerts = board.get("alerts", []) if isinstance(board.get("alerts"), list) else []
    return {
        "active_alerts": int(board.get("active_alerts", len(alerts)) or 0) if board else 0,
        "alerts": alerts,
    }


def _live_locks(root: Path, worker_id: str | None) -> list[dict[str, Any]]:
    base = _locks_root(root)
    if not base.is_dir():
        return []
    locks: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.lock.json")):
        data = _read_json(path)
        if not data or str(data.get("status") or "") == "released":
            continue
        if worker_id and str(data.get("worker_id") or "") != worker_id:
            continue
        locks.append(data)
    return locks


def _telemetry(root: Path, worker_id: str | None, fixture_dir: Path | None) -> dict[str, Any]:
    fixture = _fixture_telemetry(fixture_dir)
    if fixture:
        return fixture
    alerts = _live_alert_state(root)
    return {
        "active_alerts": int(alerts.get("active_alerts", 0) or 0),
        "alerts": alerts.get("alerts", []) if isinstance(alerts.get("alerts"), list) else [],
        "locks": _live_locks(root, worker_id),
        "tui_readiness": "unknown",
        "tui_readiness_reason": "runtime status is advisory telemetry; protocol markers remain authoritative",
    }


def classify_protocol_health(telemetry: dict[str, Any]) -> dict[str, Any]:
    locks = telemetry.get("locks", []) if isinstance(telemetry.get("locks"), list) else []
    active_nonterminal = [
        item
        for item in locks
        if isinstance(item, dict) and item.get("task_truth", {}).get("lock_state") == "active_lock"
    ]
    terminal_stale = [
        item
        for item in locks
        if isinstance(item, dict) and item.get("task_truth", {}).get("lock_state") == "stale_terminal_lock"
    ]
    if active_nonterminal:
        return {
            "state": "ERROR",
            "ok": False,
            "recoverable": False,
            "active_nonterminal_locks": len(active_nonterminal),
            "terminal_stale_locks": len(terminal_stale),
            "message": "active nonterminal lock exists",
        }
    if terminal_stale:
        return {
            "state": "WARNING",
            "ok": True,
            "recoverable": True,
            "active_nonterminal_locks": 0,
            "terminal_stale_locks": len(terminal_stale),
            "message": "terminal stale lock is recoverable telemetry",
        }
    return {
        "state": "OK",
        "ok": True,
        "recoverable": False,
        "active_nonterminal_locks": 0,
        "terminal_stale_locks": 0,
        "message": "no protocol lock errors",
    }


def classify_substrate_health(
    *,
    backend: str,
    verify: Any,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if backend.startswith("rmux"):
        return {
            "state": "CANDIDATE" if backend == "rmux-spike" and getattr(verify, "ok", False) else ("OK" if getattr(verify, "ok", False) else "ERROR"),
            "ok": bool(getattr(verify, "ok", False)),
            "backend": backend,
            "backend_type": "rmux",
            "live_enabled": is_backend_live_enabled(backend),
            "fixture_only": backend == "rmux-spike",
            "binding_confidence": (getattr(getattr(verify, "binding", None), "binding_confidence", "") or ""),
            "metadata": metadata,
        }
    return {
        "state": "OK" if getattr(verify, "ok", False) else "ERROR",
        "ok": bool(getattr(verify, "ok", False)),
        "backend": backend,
        "backend_type": "tmux",
        "live_enabled": True,
        "fixture_only": False,
        "binding_confidence": (getattr(getattr(verify, "binding", None), "binding_confidence", "") or ""),
        "metadata": metadata,
    }


def classify_runtime_telemetry(readiness: Any, telemetry: dict[str, Any]) -> dict[str, Any]:
    readiness_value = str(telemetry.get("tui_readiness") or getattr(readiness, "readiness", "unknown") or "unknown")
    return {
        "state": "TELEMETRY",
        "ok": True,
        "tui_readiness": readiness_value,
        "tui_readiness_reason": str(
            telemetry.get("tui_readiness_reason") or getattr(readiness, "reason", "") or ""
        ),
        "submit_safe": bool(telemetry.get("submit_safe", False)),
        "post_terminal_tui_busy": bool(telemetry.get("post_terminal_tui_busy")),
        "post_terminal_continuation_state": str(telemetry.get("post_terminal_continuation_state") or ""),
        "active_alerts": int(telemetry.get("active_alerts", 0) or 0),
        "alerts": telemetry.get("alerts", []) if isinstance(telemetry.get("alerts"), list) else [],
        "locks": telemetry.get("locks", []) if isinstance(telemetry.get("locks"), list) else [],
        "telemetry_only": True,
    }


def merge_health(
    protocol_health: dict[str, Any],
    substrate_health: dict[str, Any],
    runtime_telemetry: dict[str, Any],
) -> tuple[bool, list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    if not protocol_health.get("ok"):
        errors.append(str(protocol_health.get("message") or "protocol health failed"))
    if not substrate_health.get("ok"):
        errors.append("substrate health failed")
    if protocol_health.get("state") == "WARNING":
        warnings.append(str(protocol_health.get("message") or "protocol warning"))
    if runtime_telemetry.get("tui_readiness") == "unknown":
        notes.append("tui_readiness unknown is nonfatal telemetry")
    if runtime_telemetry.get("tui_readiness") == "unknown_but_submit_safe":
        notes.append("tui_readiness unknown_but_submit_safe is nonfatal telemetry")
    if runtime_telemetry.get("post_terminal_tui_busy"):
        notes.append("post_terminal_tui_busy is nonfatal session-layer telemetry")
    ok = not errors
    return ok, errors, warnings, notes


def build_rebind_health_summary(verify: Any) -> dict[str, Any]:
    ok = bool(getattr(verify, "ok", False))
    status = "current_binding_ok" if ok else "binding_invalid"
    return {
        "ok": ok,
        "status": status,
        "warnings": list(getattr(verify, "warnings", [])),
        "errors": list(getattr(verify, "errors", [])),
    }


def build_worker_health_summary(
    *,
    worker_id: str,
    backend: str,
    resolved_worker: Any | None,
    verify: Any,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    backend_type = "rmux" if backend.startswith("rmux") else "tmux"
    binding = getattr(verify, "binding", None)
    pane = (
        getattr(binding, "pane_id", "")
        or metadata.get("pane_id")
        or metadata.get("pane")
        or getattr(resolved_worker, "pane_id", "")
        or ""
    )
    port = (
        getattr(binding, "port", None)
        if getattr(binding, "port", None) is not None
        else metadata.get("port", getattr(resolved_worker, "port", None))
    )
    host = (
        getattr(binding, "host", "")
        or str(metadata.get("host") or getattr(resolved_worker, "host", "") or "")
    )
    binding_confidence = (
        getattr(binding, "binding_confidence", "")
        or str(metadata.get("binding_confidence") or getattr(resolved_worker, "binding_confidence", "") or "")
    )
    return {
        "worker_id": worker_id,
        "backend": backend,
        "backend_type": backend_type,
        "role": str(getattr(resolved_worker, "role", "") or ""),
        "pane": pane,
        "port": port,
        "host": host,
        "binding_confidence": binding_confidence,
        "allow_auto_submit": bool(getattr(resolved_worker, "allow_auto_submit", False)),
        "default_for_new_tasks": bool(getattr(resolved_worker, "default_for_new_tasks", False)),
        "dispatch_allowed": bool(getattr(resolved_worker, "dispatch_allowed", False)),
    }


def build_runtime_health_summary(
    root: Path,
    worker_id: str | None = None,
    backend: str = "tmux-reference",
    fixture_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    backend = backend or default_backend_name()
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
    resolved_worker = None
    try:
        resolved = adapter.resolve_worker(root, worker)
        resolved_worker = getattr(resolved, "worker", None)
    except RuntimeAdapterError:
        resolved_worker = None
    verify = adapter.verify_binding(root, worker)
    readiness = adapter.fetch_readiness(root, worker)
    status = adapter.observe_status(root, worker)
    metadata = adapter.collect_runtime_metadata(root, worker)
    dispatchable_visible_workers = [
        item.to_dict()
        for item in adapter.discover_workers(root)
        if bool(getattr(item, "dispatch_allowed", False))
    ]
    telemetry = _telemetry(root, worker, fixture)
    protocol_health = classify_protocol_health(telemetry)
    substrate_health = classify_substrate_health(backend=backend, verify=verify, metadata=metadata)
    runtime_telemetry = classify_runtime_telemetry(readiness, telemetry)
    worker_summary = build_worker_health_summary(
        worker_id=worker,
        backend=backend,
        resolved_worker=resolved_worker,
        verify=verify,
        metadata=metadata,
    )
    if (
        runtime_telemetry.get("tui_readiness") == "unknown"
        and protocol_health.get("ok")
        and substrate_health.get("ok")
        and bool(worker_summary.get("allow_auto_submit"))
        and bool(worker_summary.get("dispatch_allowed"))
        and str(worker_summary.get("binding_confidence") or "") == "strong"
        and int(runtime_telemetry.get("active_alerts", 0) or 0) == 0
    ):
        runtime_telemetry["tui_readiness"] = "unknown_but_submit_safe"
        runtime_telemetry["tui_readiness_reason"] = (
            "session/status did not expose ready/busy, but binding is strong, auto-submit is allowed, and no active alerts/locks are present"
        )
        runtime_telemetry["submit_safe"] = True
    rebind = build_rebind_health_summary(verify)
    ok, errors, warnings, notes = merge_health(protocol_health, substrate_health, runtime_telemetry)
    warnings.extend(list(getattr(verify, "warnings", [])))
    errors.extend(list(getattr(verify, "errors", [])))
    if errors:
        ok = False
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(root),
        "worker_id": worker,
        "worker_role": str(getattr(resolved_worker, "role", "") or ""),
        "runtime_backend": backend,
        "backend_type": "rmux" if backend.startswith("rmux") else "tmux",
        "default_backend": default_backend_name(),
        "rmux_default": str(default_backend_name()).startswith("rmux"),
        "live_enabled": is_backend_live_enabled(backend),
        "fixture_mode": bool(fixture),
        **command_support_report(),
        "ok": ok,
        "overall": "OK" if ok else "ERROR",
        "protocol_health": protocol_health,
        "substrate_health": substrate_health,
        "runtime_telemetry": runtime_telemetry,
        "runtime_status": status,
        "rebind": rebind,
        "readiness": {
            "readiness": runtime_telemetry.get("tui_readiness", "unknown"),
            "reason": runtime_telemetry.get("tui_readiness_reason", ""),
            "submit_safe": bool(runtime_telemetry.get("submit_safe", False)),
        },
        "dispatchable_visible_workers": dispatchable_visible_workers,
        "worker": worker_summary,
        "errors": errors,
        "warnings": warnings,
        "notes": notes,
    }


def build_all_runtime_health_summary(
    root: Path,
    backend: str = "tmux-reference",
    fixture_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    fixture = Path(fixture_dir).resolve() if fixture_dir else None
    if backend == "rmux-spike" and fixture is None:
        raise RuntimeAdapterError("rmux-spike fixture required; live rmux disabled in spike-1")
    adapter = get_backend(backend, fixture_dir=fixture)
    workers = [
        build_runtime_health_summary(root, worker.worker_id, backend=backend, fixture_dir=fixture)
        for worker in adapter.discover_workers(root)
    ]
    production = [
        item
        for item in workers
        if item.get("substrate_health", {}).get("metadata", {}).get("backend_type") != "test"
    ]
    ok = all(item.get("ok", False) for item in production)
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(root),
        "runtime_backend": backend,
        "default_backend": default_backend_name(),
        "rmux_default": str(default_backend_name()).startswith("rmux"),
        "live_enabled": is_backend_live_enabled(backend),
        "fixture_mode": bool(fixture),
        **command_support_report(),
        "ok": ok,
        "overall": "OK" if ok else "ERROR",
        "workers": workers,
    }
