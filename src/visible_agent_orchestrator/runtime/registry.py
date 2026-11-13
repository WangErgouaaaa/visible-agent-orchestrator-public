"""Visible worker registry loading and dispatch guard rules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..visible_registry_lock import (
    is_inactive_stale_visible_worker,
    visible_binding_alias_conflicts,
)
from .errors import RegistryError, WorkerNotFoundError

REGISTRY_RELATIVE_PATH = Path(".omx/framework/team/opencode_visible_workers.json")
NON_DISPATCHABLE_ROLES = {"test", "disabled"}
EXCLUDED_DEFAULT_STATUSES = {"test_fixed_workers", "agent_created_test_visible_worker"}


def _registry_path(root: Path, fixture_dir: Path | None = None) -> Path:
    if fixture_dir is not None:
        return Path(fixture_dir) / "opencode_visible_workers.json"
    return Path(root) / REGISTRY_RELATIVE_PATH


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"visible worker registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"visible worker registry is invalid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"visible worker registry must be a JSON object: {path}")
    return data


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_port(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def infer_worker_role(record: dict[str, Any]) -> str:
    explicit = str(record.get("role") or "").strip()
    if explicit:
        return explicit
    status = str(record.get("status") or "")
    if status in EXCLUDED_DEFAULT_STATUSES:
        return "test"
    if _coerce_bool(record.get("default_for_new_tasks")) and _coerce_bool(record.get("allow_auto_submit")):
        return "default"
    if not _coerce_bool(record.get("allow_auto_submit")):
        return "disabled"
    return "production_candidate"


def normalize_worker_record(record: dict[str, Any]) -> dict[str, Any]:
    worker = dict(record)
    pane_id = str(worker.get("pane_id") or worker.get("pane") or "")
    worker["pane_id"] = pane_id
    worker["pane"] = pane_id
    worker["port"] = _coerce_port(worker.get("port"))
    worker["host"] = str(worker.get("host") or "127.0.0.1")
    worker["backend"] = str(worker.get("backend") or "opencode_tui")
    worker["allow_auto_submit"] = _coerce_bool(worker.get("allow_auto_submit"))
    worker["default_for_new_tasks"] = _coerce_bool(worker.get("default_for_new_tasks"))
    worker["binding_confidence"] = str(worker.get("binding_confidence") or "")
    worker["worker_id"] = str(worker.get("worker_id") or "")
    worker["role"] = infer_worker_role(worker)
    return worker


def load_visible_workers(root: Path, fixture_dir: Path | None = None) -> list[dict[str, Any]]:
    data = _read_json(_registry_path(root, fixture_dir))
    workers = data.get("workers", [])
    if not isinstance(workers, list):
        raise RegistryError("visible worker registry field 'workers' must be a list")
    result: list[dict[str, Any]] = []
    for item in workers:
        if not isinstance(item, dict):
            continue
        worker = normalize_worker_record(item)
        if worker["worker_id"]:
            result.append(worker)
    for worker in result:
        if is_inactive_stale_visible_worker(worker):
            worker["binding_alias_inactive_stale"] = True
            continue
        ignored_inactive_aliases: list[str] = []
        conflicts = visible_binding_alias_conflicts(
            result,
            worker,
            ignore_inactive_stale_aliases=True,
            ignored_inactive_aliases=ignored_inactive_aliases,
        )
        if conflicts:
            worker["binding_alias_quarantined"] = True
            worker["binding_alias_conflicts"] = conflicts
        if ignored_inactive_aliases:
            ignored = sorted(set(ignored_inactive_aliases))
            worker["ignored_inactive_binding_aliases"] = ignored
            worker["binding_alias_cleanup_commands"] = [
                "./bin/omx_opencode_tui_adapter unregister-visible "
                f"--root . --worker {worker_id}"
                for worker_id in ignored
            ]
    return result


def _default_candidates(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [worker for worker in workers if worker.get("default_for_new_tasks")]


def resolve_default_worker(root: Path, fixture_dir: Path | None = None) -> dict[str, Any]:
    workers = load_visible_workers(root, fixture_dir)
    candidates = _default_candidates(workers)
    if len(candidates) != 1:
        ids = [str(item.get("worker_id") or "") for item in candidates]
        raise RegistryError(f"expected exactly one default visible worker, found {len(candidates)}: {ids}")
    validate_worker_for_dispatch(candidates[0], must_be_default=True)
    return candidates[0]


def resolve_worker_record(root: Path, worker_id: str, fixture_dir: Path | None = None) -> dict[str, Any]:
    requested = str(worker_id or "").strip()
    if not requested:
        return resolve_default_worker(root, fixture_dir)
    for worker in load_visible_workers(root, fixture_dir):
        if str(worker.get("worker_id") or "") == requested:
            return worker
    raise WorkerNotFoundError(f"visible worker not found: {requested}")


def is_auto_submit_allowed(record: dict[str, Any]) -> bool:
    return _coerce_bool(record.get("allow_auto_submit"))


def validate_worker_for_dispatch(record: dict[str, Any], *, must_be_default: bool = False) -> dict[str, Any]:
    worker = normalize_worker_record(record)
    warnings: list[str] = []
    errors: list[str] = []
    worker_id = str(worker.get("worker_id") or "")
    role = str(worker.get("role") or infer_worker_role(worker))
    status = str(worker.get("status") or "")
    alias_conflicts = worker.get("binding_alias_conflicts")

    if not worker_id:
        errors.append("worker_id is required")
    if isinstance(alias_conflicts, list) and alias_conflicts:
        errors.append(
            f"worker {worker_id} physical binding alias conflict: "
            + "; ".join(str(item) for item in alias_conflicts)
        )
    if status in EXCLUDED_DEFAULT_STATUSES or role in NON_DISPATCHABLE_ROLES:
        errors.append(f"worker {worker_id} is not dispatchable: role={role} status={status or '-'}")
    if not is_auto_submit_allowed(worker):
        errors.append(f"worker {worker_id} has allow_auto_submit=false")
    if str(worker.get("binding_confidence") or "") != "strong":
        errors.append(f"worker {worker_id} binding_confidence is not strong: {worker.get('binding_confidence') or '-'}")
    lease_id = str(worker.get("binding_lease_id") or "")
    expires_at = str(
        worker.get("binding_expires_at")
        or worker.get("expires_at")
        or ""
    )
    if not lease_id and not expires_at:
        warnings.append(
            f"worker {worker_id} legacy binding has no lease; "
            "refresh or sanctioned rebind is recommended"
        )
    elif not lease_id or not expires_at:
        errors.append(
            f"worker {worker_id} binding lease metadata is incomplete"
        )
    else:
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except ValueError:
            errors.append(
                f"worker {worker_id} binding lease expiry is invalid: {expires_at}"
            )
        else:
            if expires <= datetime.now(timezone.utc):
                errors.append(
                    f"worker {worker_id} binding lease expired at {expires_at}"
                )
    if must_be_default and not worker.get("default_for_new_tasks"):
        errors.append(f"worker {worker_id} is not default_for_new_tasks=true")
    backend = str(worker.get("backend") or "")
    if backend not in {"opencode_tui", "codex_tui"}:
        warnings.append(f"worker {worker_id} backend is {backend or '-'}, expected opencode_tui or codex_tui")
    if backend != "codex_tui" and worker.get("port") is None:
        warnings.append(f"worker {worker_id} has no numeric port")
    if backend == "codex_tui" and not str(worker.get("codex_thread") or ""):
        errors.append(f"worker {worker_id} codex_tui backend requires codex_thread binding")
    if not worker.get("pane_id"):
        warnings.append(f"worker {worker_id} has no pane id")

    return {
        "dispatch_allowed": not errors,
        "warnings": warnings,
        "errors": errors,
        "worker": worker,
    }
