"""Binding-only OpenCode visible worker registry refresh.

This module repairs stale pane/process/port bindings in the OpenCode visible
worker registry. It never submits work, sends tmux input, calls /tui/*, writes
registered-worker registries, or promotes mixed/business readiness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.opencode_visible_registry import (
    VisibleRegistryError,
    advance_binding_generations,
    derive_process_instance,
    mutate_visible_registry,
    replace_visible_worker_binding,
)
from visible_agent_orchestrator.visible_registry_lock import (
    visible_binding_alias_conflicts,
    write_json_atomic,
    write_visible_registry_mutation_audit,
)


SCHEMA_VERSION = 1
UPDATE_KIND = "opencode-visible-binding-update"
BACKEND_KIND = "opencode_tui"
BINDING_LEASE_SECONDS = 86_400
ALLOWED_BINDING_FIELDS = {
    "base_url",
    "binding_generation",
    "binding_confidence",
    "binding_created_at",
    "binding_created_by",
    "binding_evidence",
    "binding_expires_at",
    "binding_generation",
    "binding_lease_id",
    "binding_owner_run_id",
    "binding_owner_task_id",
    "binding_owner_tmux_pane",
    "binding_transaction_id",
    "binding_transaction_state",
    "binding_updated_at",
    "detected_tui_methods",
    "doc_available",
    "fencing_generation",
    "host",
    "last_verified_at",
    "listener_pids",
    "opencode_cmdline_contains_port",
    "opencode_pid",
    "opencode_pids",
    "pane_current_command",
    "pane_current_path",
    "pane_id",
    "pane_pid",
    "pane_title",
    "port",
    "server_live",
    "strong_pid_matches",
    "tmux_pane_index",
    "tmux_session",
    "tmux_window",
}
FORBIDDEN_POLICY_FIELDS = {
    "allow_auto_submit",
    "allow_business_repo_writes",
    "allow_mixed_worker",
    "backend",
    "backend_kind",
    "business_ready",
    "default_for_new_tasks",
    "mixed_ready",
    "user_started",
    "worker_id",
}
MANAGED_GENERATION_FIELDS = {
    "binding_generation",
    "fencing_generation",
}


@dataclass(frozen=True)
class OpenCodeVisibleBindingUpdateRequest:
    root: str
    worker_id: str
    opencode_visible_registry_path: str
    target_pane: str
    target_port: int
    allow_framework_registry: bool = False
    allow_binding_update: bool = False
    fixture_path: str = ""
    proposed_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_opencode_visible_binding_update_plan(request: OpenCodeVisibleBindingUpdateRequest) -> dict[str, Any]:
    evidence = _collect_evidence(request)
    scope_error = str(evidence.get("scope_error") or "")
    if scope_error:
        return _scope_blocked_plan(request, scope_error)
    root = Path(request.root).resolve()
    registry_path = Path(request.opencode_visible_registry_path).resolve()
    registry_payload = _read_json(registry_path) if registry_path.exists() else {}
    workers = _list(registry_payload.get("workers"))
    current = _find_worker(registry_payload, request.worker_id)
    panes = [_normalize_pane(item) for item in _list(evidence.get("tmux_panes"))]
    processes = [_normalize_process(item) for item in _list(evidence.get("processes"))]
    listeners = [_normalize_listener(item) for item in _list(evidence.get("listeners"))]

    blocked: list[str] = []
    warnings: list[str] = []
    if not _is_framework_opencode_visible_registry(root, registry_path):
        blocked.append(f"registry path outside allowed OpenCode visible framework registry: {registry_path}")
    if ".omx/state/team" in registry_path.as_posix():
        blocked.append(".omx/state/team registry path refused")
    if "registered-workers" in registry_path.parts:
        blocked.append("registered worker registry path refused")
    if not registry_path.exists():
        blocked.append(f"registry missing: {registry_path}")
    if not current:
        blocked.append(f"worker missing: {request.worker_id}")
    owner_lock_path = (
        root
        / ".omx"
        / "framework"
        / "team"
        / "visible-runs"
        / "_locks"
        / f"{request.worker_id}.lock.json"
    )
    owner_lock = _read_json(owner_lock_path) if owner_lock_path.exists() else {}
    if owner_lock:
        blocked.append(
            "active owner lock blocks binding update:"
            f" run={owner_lock.get('run') or '-'}"
            f" task={owner_lock.get('task') or '-'}"
        )

    backend = str(current.get("backend", current.get("backend_kind", ""))) if current else ""
    if current and backend != BACKEND_KIND:
        blocked.append(f"target worker backend must be {BACKEND_KIND}")
    current_port = _int(current.get("port")) if current else 0
    if current_port and current_port != request.target_port:
        blocked.append(f"target port {request.target_port} does not match worker registry port {current_port}")

    pane = next((item for item in panes if str(item.get("pane_id", "")) == request.target_pane), {})
    pane_pid = _int(pane.get("pane_pid"))
    descendants = _descendant_pids(processes, pane_pid)
    pane_tree_pids = sorted({pane_pid, *descendants}) if pane_pid else []
    pane_processes = [item for item in processes if _int(item.get("pid")) in set(pane_tree_pids)]
    pane_contains_opencode = any(_looks_opencode(item) for item in pane_processes)

    port_listeners = [item for item in listeners if _int(item.get("port")) == request.target_port]
    listener_pids = sorted({_int(pid) for item in port_listeners for pid in _list(item.get("pids")) if _int(pid)})
    listener_processes = [item for item in processes if _int(item.get("pid")) in set(listener_pids)]
    listener_cmdlines = [str(item.get("args", item.get("cmdline", ""))) for item in listener_processes]
    listener_maps_to_pane = bool(set(listener_pids).intersection(pane_tree_pids))
    listener_cmdline_contains_port = any(_cmdline_mentions_port(text, request.target_port) for text in listener_cmdlines)

    if not pane:
        blocked.append(f"target pane {request.target_pane} not found")
    if pane and not pane_contains_opencode:
        blocked.append(f"target pane {request.target_pane} process tree does not contain opencode")
    if pane and str(pane.get("current_path", "")) and not _path_within_root(Path(str(pane.get("current_path", ""))), root):
        blocked.append(f"target pane {request.target_pane} path is outside root: {pane.get('current_path')}")
    if not listener_pids:
        blocked.append(f"port {request.target_port} listener not found")
    if len(listener_pids) > 1:
        blocked.append(f"port {request.target_port} has ambiguous listener pids: {listener_pids}")
    if listener_pids and not listener_maps_to_pane:
        blocked.append(f"port {request.target_port} listener pid(s) {listener_pids} do not map to pane {request.target_pane}")
    if listener_pids and not listener_cmdline_contains_port:
        warnings.append(f"listener cmdline did not explicitly mention port {request.target_port}")

    proposed = _proposed_record(
        current,
        root=root,
        worker_id=request.worker_id,
        target_pane=request.target_pane,
        target_port=request.target_port,
        pane=pane,
        listener_pids=listener_pids,
        listener_cmdlines=listener_cmdlines,
        pane_tree_pids=pane_tree_pids,
        listener_maps_to_pane=listener_maps_to_pane,
        listener_cmdline_contains_port=listener_cmdline_contains_port,
        evidence=evidence,
    )
    if request.proposed_overrides:
        managed_overrides = sorted(set(request.proposed_overrides).intersection(MANAGED_GENERATION_FIELDS))
        if managed_overrides:
            blocked.append(f"managed generation fields cannot be overridden: {managed_overrides}")
        proposed.update(
            {
                key: value
                for key, value in request.proposed_overrides.items()
                if key not in MANAGED_GENERATION_FIELDS
            }
        )

    changed = _changed_fields(current, proposed)
    forbidden_changed = sorted(field for field in changed if field in FORBIDDEN_POLICY_FIELDS)
    disallowed_changed = sorted(field for field in changed if field not in ALLOWED_BINDING_FIELDS and field not in FORBIDDEN_POLICY_FIELDS)
    if forbidden_changed:
        blocked.append(f"forbidden policy fields would change: {forbidden_changed}")
    if disallowed_changed:
        blocked.append(f"non-binding fields would change: {disallowed_changed}")
    if not listener_maps_to_pane or not pane_contains_opencode or len(listener_pids) != 1:
        proposed["binding_confidence"] = "failed"

    blocked = _dedupe(blocked)
    ok = not blocked
    proposed_payload = _replace_worker(registry_payload, request.worker_id, proposed) if current else registry_payload
    return {
        "schema_version": SCHEMA_VERSION,
        "update_kind": UPDATE_KIND,
        "created_at": _utc_now(),
        "ok": ok,
        "root": str(root),
        "worker_id": request.worker_id,
        "backend_kind": BACKEND_KIND,
        "registry_path": str(registry_path),
        "target_pane": request.target_pane,
        "target_port": request.target_port,
        "current_registry_record": current,
        "proposed_registry_record": proposed,
        "changed_fields": changed,
        "forbidden_fields_unchanged": not forbidden_changed,
        "forbidden_changed_fields": forbidden_changed,
        "disallowed_changed_fields": disallowed_changed,
        "pre_hash": _sha256_file(registry_path),
        "proposed_hash": _sha256_json(proposed_payload) if current else "",
        "would_write_registry": False,
        "would_execute_live": False,
        "requires_allow_framework_registry": True,
        "requires_allow_binding_update": True,
        "safe_for_binding_update": ok,
        "pane": pane,
        "pane_pid": pane_pid,
        "pane_process_tree_pids": pane_tree_pids,
        "pane_process_tree_contains_opencode": pane_contains_opencode,
        "listener_pids": listener_pids,
        "listener_cmdlines": listener_cmdlines,
        "port_pid_maps_to_pane": listener_maps_to_pane,
        "opencode_cmdline_contains_port": listener_cmdline_contains_port,
        "blocked_reasons": blocked,
        "warnings": _dedupe(warnings),
        "owner_lock": owner_lock,
        "safe_next_actions": _safe_next_actions(ok),
        "forbidden_next_actions": _forbidden_next_actions(),
        "evidence": [
            {"source": "opencode_visible_registry", "detail": {"worker_count": len(workers)}},
            {"source": "target_pane_process_tree", "detail": {"pane_id": request.target_pane, "pids": pane_tree_pids}},
            {"source": "target_port_listener", "detail": {"port": request.target_port, "listener_pids": listener_pids}},
        ],
        "no_live_actions_performed": True,
        "no_registered_registry_writes": True,
    }


def _scope_blocked_plan(
    request: OpenCodeVisibleBindingUpdateRequest,
    scope_error: str,
) -> dict[str, Any]:
    root = Path(request.root).resolve()
    registry_path = Path(request.opencode_visible_registry_path).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "update_kind": UPDATE_KIND,
        "created_at": _utc_now(),
        "ok": False,
        "root": str(root),
        "worker_id": request.worker_id,
        "backend_kind": BACKEND_KIND,
        "registry_path": str(registry_path),
        "target_pane": request.target_pane,
        "target_port": request.target_port,
        "current_registry_record": {},
        "proposed_registry_record": {},
        "changed_fields": [],
        "forbidden_fields_unchanged": True,
        "forbidden_changed_fields": [],
        "disallowed_changed_fields": [],
        "pre_hash": "",
        "proposed_hash": "",
        "would_write_registry": False,
        "would_execute_live": False,
        "requires_allow_framework_registry": True,
        "requires_allow_binding_update": True,
        "safe_for_binding_update": False,
        "pane": {},
        "pane_pid": 0,
        "pane_process_tree_pids": [],
        "pane_process_tree_contains_opencode": False,
        "listener_pids": [],
        "listener_cmdlines": [],
        "port_pid_maps_to_pane": False,
        "opencode_cmdline_contains_port": False,
        "blocked_reasons": [scope_error],
        "warnings": [],
        "owner_lock": {},
        "safe_next_actions": _safe_next_actions(False),
        "forbidden_next_actions": _forbidden_next_actions(),
        "evidence": [
            {
                "source": "caller_tmux_scope",
                "detail": {"TMUX_PANE": str(os.environ.get("TMUX_PANE") or "")},
            }
        ],
        "no_live_actions_performed": True,
        "no_registered_registry_writes": True,
    }


def _binding_update_blocked_result(
    request: OpenCodeVisibleBindingUpdateRequest,
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    blocked = list(plan["blocked_reasons"])
    if not request.allow_framework_registry:
        blocked.append("missing --allow-framework-registry")
    if not request.allow_binding_update:
        blocked.append("missing --allow-binding-update")
    blocked = _dedupe(blocked)
    if blocked:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": bool(request.allow_binding_update),
            "wrote_registry": False,
            "registry_path": plan["registry_path"],
            "worker_id": request.worker_id,
            "changed_fields": plan["changed_fields"],
            "blocked_reasons": blocked,
            "warnings": plan["warnings"],
            "would_execute_live": False,
            "no_live_actions_performed": True,
        }
    return None


def _binding_generation(record: dict[str, Any]) -> int:
    try:
        return max(0, int(record.get("binding_generation") or 0))
    except (TypeError, ValueError):
        return 0


def _binding_expiration() -> str:
    raw = str(os.environ.get("OMX_VISIBLE_BINDING_LEASE_SECONDS") or "").strip()
    try:
        seconds = max(300, int(raw)) if raw else BINDING_LEASE_SECONDS
    except ValueError:
        seconds = BINDING_LEASE_SECONDS
    expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _binding_token(worker_id: str, action: str) -> str:
    seed = (
        f"{worker_id}\0{action}\0{time.time_ns()}\0{os.getpid()}\0"
        f"{os.environ.get('TMUX_PANE', '')}"
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _fenced_binding_record(
    current: dict[str, Any],
    proposed: dict[str, Any],
) -> dict[str, Any]:
    worker_id = str(proposed.get("worker_id") or current.get("worker_id") or "")
    now = _utc_now()
    expires_at = _binding_expiration()
    created_by = f"omx_worker_backend:{UPDATE_KIND}"
    owner_run_id = str(os.environ.get("OMX_RUN_ID") or "")
    owner_task_id = str(os.environ.get("OMX_TASK_ID") or "")
    return {
        **proposed,
        "binding_generation": max(1, _binding_generation(current) + 1),
        "binding_lease_id": _binding_token(worker_id, "lease"),
        "binding_created_at": now,
        "binding_expires_at": expires_at,
        "binding_created_by": created_by,
        "binding_owner_run_id": owner_run_id,
        "binding_owner_task_id": owner_task_id,
        "binding_owner_tmux_pane": str(os.environ.get("TMUX_PANE") or ""),
        "binding_transaction_id": _binding_token(worker_id, UPDATE_KIND),
        "binding_transaction_state": "committed",
    }


def _commit_opencode_visible_binding_update(
    request: OpenCodeVisibleBindingUpdateRequest,
    plan: dict[str, Any],
) -> dict[str, Any]:
    registry_path = Path(request.opencode_visible_registry_path).resolve()
    mutation_records: dict[str, dict[str, Any]] = {}

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        owner_lock_path = (
            Path(request.root).resolve()
            / ".omx"
            / "framework"
            / "team"
            / "visible-runs"
            / "_locks"
            / f"{request.worker_id}.lock.json"
        )
        owner_lock = (
            _read_json(owner_lock_path)
            if owner_lock_path.exists()
            else {}
        )
        if owner_lock:
            raise VisibleRegistryError(
                "active owner lock blocks binding update:"
                f" run={owner_lock.get('run') or '-'}"
                f" task={owner_lock.get('task') or '-'}"
            )
        current = _find_worker(payload, request.worker_id)
        if not current:
            raise VisibleRegistryError(
                f"visible worker not found: {request.worker_id}"
            )
        proposed = _fenced_binding_record(
            current,
            dict(plan["proposed_registry_record"]),
        )
        mutation_records["current"] = current
        mutation_records["proposed"] = proposed
        ignored_inactive_aliases: list[str] = []
        alias_conflicts = visible_binding_alias_conflicts(
            _list(payload.get("workers")),
            proposed,
            ignore_worker_id=request.worker_id,
            ignore_inactive_stale_aliases=True,
            ignored_inactive_aliases=ignored_inactive_aliases,
        )
        if alias_conflicts:
            raise VisibleRegistryError(
                "physical binding alias conflict: "
                + "; ".join(alias_conflicts)
            )
        if ignored_inactive_aliases:
            mutation_records["ignored_alias_workers"] = sorted(
                set(ignored_inactive_aliases)
            )
        expected_instance_id = str(
            derive_process_instance(current).get("instance_id") or ""
        )
        return replace_visible_worker_binding(
            payload,
            worker_id=request.worker_id,
            replacement_record=proposed,
            expected_instance_id=expected_instance_id,
            controller_id=str(proposed.get("tmux_session") or ""),
        )

    try:
        mutation = mutate_visible_registry(
            registry_path,
            mutate,
            expected_pre_hash=plan["pre_hash"],
            backup_factory=_create_backup,
        )
    except VisibleRegistryError as exc:
        current = mutation_records.get(
            "current",
            dict(plan["current_registry_record"]),
        )
        proposed = mutation_records.get(
            "proposed",
            dict(plan["proposed_registry_record"]),
        )
        audit_path = ""
        try:
            audit_path = str(
                write_visible_registry_mutation_audit(
                    registry_path,
                    worker_id=request.worker_id,
                    action=UPDATE_KIND,
                    old_record=current,
                    new_record=proposed,
                    cas_result="mutation_rejected",
                    reason=str(exc),
                    result="rejected",
                    transaction_id=str(
                        proposed.get("binding_transaction_id") or ""
                    ),
                )
            )
        except OSError:
            audit_path = ""
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "attempted": True,
            "wrote_registry": False,
            "registry_path": str(registry_path),
            "worker_id": request.worker_id,
            "changed_fields": _changed_fields(current, proposed),
            "blocked_reasons": [str(exc)],
            "warnings": plan["warnings"],
            "mutation_audit_path": audit_path,
            "would_execute_live": False,
            "no_live_actions_performed": True,
        }

    current = mutation_records["current"]
    proposed = _find_worker(mutation["after"], request.worker_id)
    transaction_id = str(proposed.get("binding_transaction_id") or "")
    sidecar_path = (
        registry_path.parent
        / f"opencode_visible_binding_{request.worker_id}.json"
    )
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "worker_id": request.worker_id,
        "host": proposed.get("host"),
        "port": proposed.get("port"),
        "pane_id": proposed.get("pane_id"),
        "pane_pid": proposed.get("pane_pid"),
        "tmux_session": proposed.get("tmux_session"),
        "tmux_window": proposed.get("tmux_window"),
        "tmux_pane_index": proposed.get("tmux_pane_index"),
        "opencode_pids": proposed.get("opencode_pids", []),
        "listener_pids": proposed.get("listener_pids", []),
        "binding_confidence": proposed.get("binding_confidence"),
        "binding_generation": proposed.get("binding_generation"),
        "binding_lease_id": proposed.get("binding_lease_id"),
        "binding_expires_at": proposed.get("binding_expires_at"),
        "transaction_id": transaction_id,
        "transaction_state": "committed",
        "canonical_registry": str(registry_path),
    }
    warnings = list(plan["warnings"])
    try:
        write_json_atomic(sidecar_path, sidecar)
    except OSError as exc:
        warnings.append(f"binding sidecar write failed: {exc}")
    audit_path = ""
    try:
        audit_path = str(
            write_visible_registry_mutation_audit(
                registry_path,
                worker_id=request.worker_id,
                action=UPDATE_KIND,
                old_record=current,
                new_record=proposed,
                cas_result="locked_plan_revalidated",
                reason="authorized visible binding update applied",
                result="applied",
                transaction_id=transaction_id,
            )
        )
    except OSError as exc:
        warnings.append(f"binding mutation audit write failed: {exc}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "attempted": True,
        "wrote_registry": True,
        "atomic_write": True,
        "backup_path": mutation["backup_path"],
        "registry_path": str(registry_path),
        "worker_id": request.worker_id,
        "changed_fields": _changed_fields(current, proposed),
        "pre_hash": mutation["pre_hash"],
        "post_hash": mutation["post_hash"],
        "blocked_reasons": [],
        "warnings": _dedupe(warnings),
        "binding_generation": proposed.get("binding_generation"),
        "binding_lease_id": proposed.get("binding_lease_id"),
        "binding_transaction_id": transaction_id,
        "binding_sidecar_path": str(sidecar_path),
        "mutation_audit_path": audit_path,
        "would_execute_live": False,
        "no_live_actions_performed": True,
        "no_registered_registry_writes": True,
    }


def write_opencode_visible_binding_update(
    request: OpenCodeVisibleBindingUpdateRequest,
) -> dict[str, Any]:
    plan = build_opencode_visible_binding_update_plan(request)
    blocked = _binding_update_blocked_result(request, plan)
    if blocked is not None:
        return blocked
    return _commit_opencode_visible_binding_update(request, plan)


def validate_opencode_visible_binding_update_record(payload: dict[str, Any]) -> dict[str, Any]:
    blocked: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        blocked.append("schema_version must be 1")
    if payload.get("update_kind") != UPDATE_KIND:
        blocked.append(f"update_kind must be {UPDATE_KIND}")
    if payload.get("would_execute_live") is not False:
        blocked.append("would_execute_live must be false")
    if payload.get("would_write_registry") is not False:
        blocked.append("plan would_write_registry must be false")
    if payload.get("forbidden_fields_unchanged") is not True:
        blocked.append("forbidden fields must remain unchanged")
    for field_name in _list(payload.get("changed_fields")):
        if str(field_name) not in ALLOWED_BINDING_FIELDS:
            blocked.append(f"changed field is not binding-only: {field_name}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not _dedupe(blocked),
        "blocked_reasons": _dedupe(blocked),
        "warnings": ["validation is source-only and does not grant live retry authorization"],
    }


def _collect_evidence(request: OpenCodeVisibleBindingUpdateRequest) -> dict[str, Any]:
    if request.fixture_path:
        fixture = Path(request.fixture_path).resolve()
        return _read_json(fixture / "binding-input.json")
    root = Path(request.root).resolve()
    caller_pane = str(os.environ.get("TMUX_PANE") or "")
    if not re.fullmatch(r"%\d+", caller_pane):
        return {
            "scope_error": (
                "TMUX_PANE is required for caller-scoped binding discovery"
            ),
            "tmux_panes": [],
            "processes": [],
            "listeners": [],
            "doc_available": None,
            "detected_tui_methods": None,
        }
    try:
        panes = _tmux_panes(root, caller_pane, request.target_pane)
    except ValueError as exc:
        return {
            "scope_error": str(exc),
            "tmux_panes": [],
            "processes": [],
            "listeners": [],
            "doc_available": None,
            "detected_tui_methods": None,
        }
    return {
        "tmux_panes": panes,
        "processes": _processes(root),
        "listeners": _listeners(root),
        "doc_available": None,
        "detected_tui_methods": None,
    }


def _proposed_record(
    current: dict[str, Any],
    *,
    root: Path,
    worker_id: str,
    target_pane: str,
    target_port: int,
    pane: dict[str, Any],
    listener_pids: list[int],
    listener_cmdlines: list[str],
    pane_tree_pids: list[int],
    listener_maps_to_pane: bool,
    listener_cmdline_contains_port: bool,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    now = _utc_now()
    strong_pid_matches = sorted(set(listener_pids).intersection(pane_tree_pids))
    detected_methods = evidence.get("detected_tui_methods")
    if not isinstance(detected_methods, list):
        detected_methods = _list(current.get("detected_tui_methods"))
    doc_available = evidence.get("doc_available")
    if doc_available is None:
        doc_available = bool(current.get("doc_available", True))
    title = str(pane.get("pane_title", "")) or str(current.get("pane_title", ""))
    return advance_binding_generations({
        **current,
        "worker_id": worker_id,
        "host": "127.0.0.1",
        "base_url": f"http://127.0.0.1:{target_port}",
        "port": target_port,
        "pane_id": target_pane,
        "pane_pid": _int(pane.get("pane_pid")),
        "pane_title": title,
        "pane_current_command": str(pane.get("current_command", "")),
        "pane_current_path": str(pane.get("current_path", root)),
        "tmux_session": str(pane.get("session_name", "")) or _split_location(pane).get("session", str(current.get("tmux_session", ""))),
        "tmux_window": _split_location(pane).get("window", current.get("tmux_window", "")),
        "tmux_pane_index": _split_location(pane).get("pane", current.get("tmux_pane_index", "")),
        "opencode_pids": list(listener_pids),
        "opencode_pid": listener_pids[0] if len(listener_pids) == 1 else 0,
        "listener_pids": list(listener_pids),
        "strong_pid_matches": strong_pid_matches,
        "binding_confidence": "strong" if listener_maps_to_pane and len(listener_pids) == 1 else "failed",
        "server_live": bool(listener_pids),
        "doc_available": bool(doc_available),
        "detected_tui_methods": list(detected_methods),
        "opencode_cmdline_contains_port": bool(listener_cmdline_contains_port),
        "last_verified_at": now,
        "binding_updated_at": now,
        "binding_evidence": {
            "update_kind": UPDATE_KIND,
            "root": str(root),
            "pane_process_tree_pids": pane_tree_pids,
            "listener_cmdlines": listener_cmdlines,
            "no_live_actions_performed": True,
        },
    })


def _tmux_panes(
    root: Path,
    pane_target: str | None = None,
    target_pane: str | None = None,
) -> list[dict[str, Any]]:
    target = str(pane_target or os.environ.get("TMUX_PANE") or "")
    if not re.fullmatch(r"%\d+", target):
        raise ValueError(
            "TMUX_PANE is required for caller-scoped binding discovery"
        )
    display = _run(
        [
            "tmux",
            "display-message",
            "-t",
            target,
            "-p",
            "#{pane_id}\t#{session_name}\t#{window_index}",
        ],
        root,
    )
    if display.get("returncode") != 0:
        detail = str(display.get("stderr") or "").strip() or "no pane evidence returned"
        raise ValueError(
            f"TMUX_PANE {target} display-message failed: {detail}"
        )
    display_lines = [
        line.strip()
        for line in str(display.get("stdout") or "").splitlines()
        if line.strip()
    ]
    display_parts = display_lines[0].split("\t") if len(display_lines) == 1 else []
    if (
        len(display_parts) != 3
        or display_parts[0] != target
        or not display_parts[1]
        or not display_parts[2]
    ):
        raise ValueError(
            f"TMUX_PANE {target} did not resolve to its exact tmux pane"
        )
    caller_session, caller_window = display_parts[1], display_parts[2]
    result = _run(
        [
            "tmux",
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t"
            "#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}\t"
            "#{session_name}\t#{window_index}\t#{pane_index}",
        ],
        root,
    )
    if result.get("returncode") != 0:
        detail = str(result.get("stderr") or "").strip() or "no pane evidence returned"
        raise ValueError(
            f"TMUX_PANE {target} list-panes failed: {detail}"
        )
    panes: list[dict[str, Any]] = []
    for line in str(result.get("stdout") or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 8:
            panes.append(
                {
                    "pane_id": parts[0],
                    "pane_pid": _int(parts[1]),
                    "current_command": parts[2],
                    "current_path": parts[3],
                    "location": parts[4],
                    "session_name": parts[5],
                    "window_index": parts[6],
                    "pane_index": parts[7],
                    "runtime_substrate": "tmux",
                }
            )
    caller = next(
        (pane for pane in panes if str(pane.get("pane_id") or "") == target),
        {},
    )
    if (
        str(caller.get("session_name") or "") != caller_session
        or str(caller.get("window_index") or "") != caller_window
    ):
        raise ValueError(
            f"TMUX_PANE {target} did not resolve to its exact tmux pane"
        )
    expected_target = str(target_pane or target)
    target_record = next(
        (
            pane
            for pane in panes
            if str(pane.get("pane_id") or "") == expected_target
        ),
        {},
    )
    if (
        str(target_record.get("session_name") or "") != caller_session
        or str(target_record.get("window_index") or "") != caller_window
    ):
        raise ValueError(
            f"target pane {expected_target} is not in TMUX_PANE {target} caller window"
        )
    return panes


def _processes(root: Path) -> list[dict[str, Any]]:
    result = _run(["ps", "-eo", "pid,ppid,pgid,stat,comm,args"], root)
    rows: list[dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines()[1:]:
        parts = line.strip().split(None, 5)
        if len(parts) >= 6:
            rows.append({"pid": _int(parts[0]), "ppid": _int(parts[1]), "pgid": _int(parts[2]), "stat": parts[3], "comm": parts[4], "args": parts[5]})
    return rows


def _listeners(root: Path) -> list[dict[str, Any]]:
    result = _run(["ss", "-ltnp"], root)
    if result.get("returncode") != 0:
        result = _run(["netstat", "-ltnp"], root)
    rows: list[dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines():
        ports = [int(item) for item in re.findall(r":(\d+)\b", line)]
        pids = [int(item) for item in re.findall(r"pid=(\d+)", line)]
        if not pids:
            pids = [int(item) for item in re.findall(r"\s(\d+)/", line)]
        for port in ports[-1:]:
            rows.append({"port": port, "pids": pids, "line": line})
    return rows


def _run(argv: list[str], cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=30, check=False)
        return {"argv": argv, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "returncode": 1, "stdout": "", "stderr": str(exc)}


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _find_worker(registry: dict[str, Any], worker_id: str) -> dict[str, Any]:
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            return dict(item)
    return {}


def _replace_worker(registry: dict[str, Any], worker_id: str, replacement: dict[str, Any]) -> dict[str, Any]:
    workers = []
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            workers.append(replacement)
        else:
            workers.append(item)
    return {**registry, "workers": workers}


def _is_framework_opencode_visible_registry(root: Path, registry_path: Path) -> bool:
    allowed = root / ".omx" / "framework" / "team" / "opencode_visible_workers.json"
    return registry_path == allowed.resolve()


def _normalize_pane(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    text = str(item)
    match = re.search(r"(?P<loc>\S+)\s+(?P<pane>%\d+)\s+pid=(?P<pid>\d+)\s+command=(?P<cmd>\S+)\s+path=(?P<path>.*)", text)
    if not match:
        return {}
    return {"location": match.group("loc"), "pane_id": match.group("pane"), "pane_pid": _int(match.group("pid")), "current_command": match.group("cmd"), "current_path": match.group("path")}


def _normalize_process(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    parts = str(item).strip().split(None, 5)
    if len(parts) >= 6 and parts[0].isdigit():
        return {"pid": _int(parts[0]), "ppid": _int(parts[1]), "pgid": _int(parts[2]), "stat": parts[3], "comm": parts[4], "args": parts[5]}
    return {}


def _normalize_listener(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    text = str(item)
    ports = [int(value) for value in re.findall(r":(\d+)\b", text)]
    pids = [int(value) for value in re.findall(r"pid=(\d+)", text)]
    return {"port": ports[-1] if ports else 0, "pids": pids, "line": text}


def _descendant_pids(processes: list[dict[str, Any]], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for item in processes:
        children.setdefault(_int(item.get("ppid")), []).append(_int(item.get("pid")))
    seen: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _looks_opencode(process: dict[str, Any]) -> bool:
    comm = Path(str(process.get("comm", "")).split()[0]).name.lower()
    args = str(process.get("args", "")).strip()
    argv0 = args.split(None, 1)[0] if args else ""
    argv0_base = Path(argv0).name.lower()
    return comm == "opencode" or argv0_base == "opencode"


def _cmdline_mentions_port(text: str, port: int) -> bool:
    return bool(re.search(rf"(^|\s)--port[=\s]{port}(\s|$)", text)) or f":{port}" in text


def _split_location(pane: dict[str, Any]) -> dict[str, Any]:
    out = {"session": "", "window": "", "pane": ""}
    loc = str(pane.get("location", ""))
    match = re.match(r"(?P<session>.*):(?P<window>\d+)\.(?P<pane>\d+)$", loc)
    if match:
        out.update(match.groupdict())
    if pane.get("session_name"):
        out["session"] = str(pane.get("session_name"))
    if pane.get("window_index") is not None:
        out["window"] = str(pane.get("window_index"))
    if pane.get("pane_index") is not None:
        out["pane"] = str(pane.get("pane_index"))
    return out


def _changed_fields(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _create_backup(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".backup")
    index = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup.{index}")
        index += 1
    shutil.copy2(path, backup)
    return backup


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_json(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return ["run opencode-visible-binding-update with explicit allow flags", "rerun identity diagnostics without live submit"]
    return ["fix pane/process/port binding evidence and rerun opencode-visible-binding-update-plan"]


def _forbidden_next_actions() -> list[str]:
    return [
        "live retry",
        "tmux input",
        "direct /tui/*",
        "registered worker registry write",
        ".omx/state/team write",
        "mixed_ready/default/policy flag change",
        "business repo write",
        "Obsidian write",
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = [
    "OpenCodeVisibleBindingUpdateRequest",
    "build_opencode_visible_binding_update_plan",
    "validate_opencode_visible_binding_update_record",
    "write_opencode_visible_binding_update",
]
