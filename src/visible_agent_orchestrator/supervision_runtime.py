"""Host-local canonical coordination state for effect-capable supervisors."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from visible_agent_orchestrator.collaboration_profile import collaboration_profile_digest
from visible_agent_orchestrator.supervision_engine import evaluate_supervision
from visible_agent_orchestrator.supervision_policy import supervision_policy_digest
from visible_agent_orchestrator.terminal_acceptance import (
    FLOW_MARKERS,
    TERMINAL_REPORT_SETTLE_SECONDS,
)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EDGE_CIRCUIT_FAILURE_THRESHOLD = 2
# ponytail: fixed conservative threshold; move it into policy only if edge-specific tuning is needed.


class SupervisionControlError(RuntimeError):
    """Base error for canonical supervisor control state."""


class SupervisionControlConflict(SupervisionControlError):
    """A current lease or edge claim prevents the requested transition."""


class SupervisionRuntimeError(SupervisionControlError):
    """A supervisor output lifecycle or bounded-run contract is invalid."""


def run_supervisor(
    root: Path | str,
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    policy: dict[str, Any],
    snapshots: list[dict[str, Any]] | None,
    output_dir: Path | str,
    grant: dict[str, Any] | None = None,
    max_cycles: int | None = None,
    resume: bool = False,
    supervisor_instance_id: str = "supervisor-local",
    allow_actuation: bool = False,
    build_digest: str = "",
    worker_dispatcher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    receipt_lookup: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    allowed_worker_effect_classes: set[str] | None = None,
    snapshot_reader: Callable[[], dict[str, Any]] | None = None,
    grant_reader: Callable[[], dict[str, Any]] | None = None,
    interval_seconds: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run bounded observe/shadow cycles and persist only supervisor artifacts."""
    if not snapshots and snapshot_reader is None:
        raise SupervisionRuntimeError("supervisor requires at least one evidence snapshot")
    if type(interval_seconds) not in {int, float} or interval_seconds < 0:
        raise SupervisionRuntimeError("interval_seconds must be non-negative")
    _safe_id(supervisor_instance_id, "supervisor_instance_id")
    cycle_limit = int(policy.get("observation_limits", {}).get("max_cycles") or 0)
    if max_cycles is not None:
        if type(max_cycles) is not int or max_cycles < 1:
            raise SupervisionRuntimeError("max_cycles must be a positive integer")
        cycle_limit = min(cycle_limit, max_cycles)
    if cycle_limit < 1:
        raise SupervisionRuntimeError("supervision policy max_cycles is invalid")

    root_path = Path(root).resolve()
    output = _prepare_output_dir(
        output_dir,
        identity={
            "run_id": manifest["run_id"],
            "workflow_revision": manifest["workflow_revision"],
            "profile_digest": collaboration_profile_digest(profile),
            "policy_digest": supervision_policy_digest(policy),
            "build_digest": build_digest or _build_digest(),
        },
        resume=resume,
    )
    actuation_permitted = (
        allow_actuation
        and str(policy.get("mode") or "") == "preauthorized_followups"
        and grant is not None
    )
    allowed_worker_effects = set(
        allowed_worker_effect_classes or {"read_only_worker"}
    )
    if not allowed_worker_effects.issubset(
        {"read_only_worker", "write_isolated"}
    ):
        raise SupervisionRuntimeError("worker effect capability is invalid")
    lease: dict[str, Any] | None = None

    decisions: list[dict[str, Any]] = []
    notifications_emitted = 0
    started_at = clock()
    last_snapshot: dict[str, Any] = {}
    elapsed_limit_ms = int(policy.get("observation_limits", {}).get("max_elapsed_ms") or 0)
    for cycle in range(1, cycle_limit + 1):
        if snapshot_reader is not None:
            if cycle > 1 and interval_seconds:
                time.sleep(float(interval_seconds))
            if (
                cycle > 1
                and (clock() - started_at) * 1000 >= elapsed_limit_ms
            ):
                decision_started_at = time.perf_counter()
                decision = _budget_exhausted_decision(
                    policy,
                    lease_epoch=int((lease or {}).get("lease_epoch") or 0),
                    snapshot=last_snapshot,
                )
                decision = {
                    **decision,
                    "decision_latency_ms": _elapsed_ms(
                        decision_started_at,
                        time.perf_counter(),
                    ),
                }
                notifications_emitted += int(
                    _write_cycle_artifacts(output, cycle, last_snapshot, decision)
                )
                decisions.append(decision)
                break
            snapshot = snapshot_reader()
        else:
            assert snapshots
            snapshot = snapshots[min(cycle - 1, len(snapshots) - 1)]
        last_snapshot = snapshot
        cycle_grant = grant_reader() if grant_reader is not None else grant
        if grant_reader is not None and not isinstance(cycle_grant, dict):
            cycle_grant = {"state": "revoked"}
        decision_started_at = time.perf_counter()
        decision = evaluate_supervision(
            profile=profile,
            manifest=manifest,
            policy=policy,
            grant=cycle_grant,
            snapshot=snapshot,
            lease_epoch=int((lease or {}).get("lease_epoch") or 0),
        )
        decision = {
            **decision,
            "decision_latency_ms": _elapsed_ms(
                decision_started_at,
                time.perf_counter(),
            ),
        }
        if decision.get("disposition") == "act" and not actuation_permitted:
            decision = {
                **decision,
                "disposition": "propose",
                "reason": "actuation_cli_not_enabled",
                "would_act": False,
            }
        elif decision.get("disposition") == "act":
            if _is_local_effect(str(decision.get("effect_class") or "")):
                proposal = _write_local_proposal(output, manifest, snapshot, decision)
                decision = {
                    **decision,
                    "executed_action": "local_proposal",
                    "proposal_path": str(proposal),
                }
            elif (
                _is_worker_effect(str(decision.get("effect_class") or ""))
                and str(decision.get("effect_class") or "")
                not in allowed_worker_effects
            ):
                decision = {
                    **decision,
                    "disposition": "propose",
                    "reason": "worker_effect_cli_not_enabled",
                    "would_act": False,
                }
            elif _is_worker_effect(str(decision.get("effect_class") or "")) and (
                _edge_circuit_open(
                    root_path,
                    profile=profile,
                    manifest=manifest,
                    decision=decision,
                )
            ):
                decision = {
                    **decision,
                    "disposition": "escalate",
                    "reason": "edge_circuit_open",
                    "would_act": False,
                }
            elif _is_worker_effect(str(decision.get("effect_class") or "")):
                if lease is None:
                    lease = acquire_actuation_lease(
                        root_path,
                        run_id=str(manifest["run_id"]),
                        supervisor_instance_id=supervisor_instance_id,
                        policy_digest=supervision_policy_digest(policy),
                        build_digest=build_digest or _build_digest(),
                        ttl_seconds=60,
                    )
                    decision_started_at = time.perf_counter()
                    decision = evaluate_supervision(
                        profile=profile,
                        manifest=manifest,
                        policy=policy,
                        grant=cycle_grant,
                        snapshot=snapshot,
                        lease_epoch=int(lease["lease_epoch"]),
                    )
                    decision = {
                        **decision,
                        "decision_latency_ms": _elapsed_ms(
                            decision_started_at,
                            time.perf_counter(),
                        ),
                    }
                claim = claim_edge(
                    root_path,
                    run_id=str(manifest["run_id"]),
                    workflow_revision=str(manifest["workflow_revision"]),
                    edge_id=str(decision["selected_edge_id"]),
                    logical_action_id=str(decision["logical_action_id"]),
                    lease=lease,
                )
                action = write_action_intent(
                    root_path,
                    run_id=str(manifest["run_id"]),
                    logical_action_id=str(decision["logical_action_id"]),
                    claim=claim,
                    handoff=_structured_handoff(manifest, decision, snapshot, claim),
                )
                if action.get("_existing") and action["state"] == "accepted":
                    decision = {
                        **decision,
                        "disposition": "observe",
                        "reason": "action_already_accepted",
                        "would_act": False,
                        "executed_action": "already_accepted",
                        "control_action_state": action["state"],
                    }
                elif action.get("_existing"):
                    reconciled = _reconcile_durable_action(
                        root_path,
                        run_id=str(manifest["run_id"]),
                        logical_action_id=str(decision["logical_action_id"]),
                        handoff=action["handoff"],
                        receipt_lookup=receipt_lookup,
                    )
                    decision = _reconciled_decision(decision, reconciled)
                elif worker_dispatcher is None:
                    decision = {
                        **decision,
                        "executed_action": "intent_durable",
                        "control_action_state": action["state"],
                    }
                else:
                    try:
                        receipt = worker_dispatcher(action["handoff"])
                    except Exception:
                        reconciled = _reconcile_durable_action(
                            root_path,
                            run_id=str(manifest["run_id"]),
                            logical_action_id=str(decision["logical_action_id"]),
                            handoff=action["handoff"],
                            receipt_lookup=receipt_lookup,
                        )
                        decision = _reconciled_decision(decision, reconciled)
                    else:
                        accepted = record_action_receipt(
                            root_path,
                            run_id=str(manifest["run_id"]),
                            logical_action_id=str(decision["logical_action_id"]),
                            receipt=receipt,
                        )
                        decision = {
                            **decision,
                            "executed_action": "accepted",
                            "control_action_state": accepted["state"],
                            "receipt": accepted["receipt"],
                        }
        if _tracks_edge_health(decision):
            edge_health = _record_edge_health(
                root_path,
                profile=profile,
                manifest=manifest,
                decision=decision,
            )
            if edge_health:
                decision = {**decision, "edge_health": edge_health}
        notifications_emitted += int(
            _write_cycle_artifacts(output, cycle, snapshot, decision)
        )
        decisions.append(decision)
        if decision.get("disposition") in {"escalate", "stop"}:
            break

    last = decisions[-1]
    result = {
        "schema_version": 1,
        "supervisor_kind": "bounded_run_supervisor",
        "run_id": manifest["run_id"],
        "workflow_revision": manifest["workflow_revision"],
        "mode": policy["mode"],
        "actuation_enabled": actuation_permitted,
        "cycles": len(decisions),
        "metrics": _metrics(decisions, notifications_emitted),
        "last_decision": last,
        "output_dir": str(output),
    }
    _write_json_atomic(output / "supervisor_state.json", result)
    _write_summary(output, result)
    return result


def _reconcile_durable_action(
    root: Path,
    *,
    run_id: str,
    logical_action_id: str,
    handoff: dict[str, Any],
    receipt_lookup: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    receipt: dict[str, Any] | None = None
    if receipt_lookup is not None:
        try:
            receipt = receipt_lookup(handoff)
        except Exception:
            receipt = None
    try:
        return reconcile_action(
            root,
            run_id=run_id,
            logical_action_id=logical_action_id,
            receipt=receipt,
        )
    except SupervisionControlError:
        return reconcile_action(
            root,
            run_id=run_id,
            logical_action_id=logical_action_id,
        )


def _reconciled_decision(
    decision: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    if action.get("state") == "accepted":
        return {
            **decision,
            "disposition": "observe",
            "reason": "action_receipt_reconciled",
            "would_act": False,
            "executed_action": "accepted",
            "control_action_state": action["state"],
            "receipt": action["receipt"],
        }
    return {
        **decision,
        "disposition": "escalate",
        "reason": "delivery_uncertain",
        "would_act": False,
        "executed_action": "delivery_uncertain",
        "control_action_state": action["state"],
    }


def build_artifact_snapshot(
    root: Path | str,
    *,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Read declared task artifacts without inferring state from worker UI."""
    root_path = Path(root).resolve()
    lanes: dict[str, dict[str, Any]] = {}
    health: dict[str, bool] = {}
    for lane in manifest.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        node_id = str(lane.get("node_id") or "")
        task_dir_value = str(lane.get("task_dir") or "")
        if not task_dir_value:
            raise SupervisionRuntimeError(
                f"artifact observation requires task_dir for lane {node_id}"
            )
        task_dir = _declared_task_dir(root_path, task_dir_value)
        task_metadata = _read_json(task_dir / "task.json")
        fence = _read_json(task_dir / "acceptance_fence.json")
        report_path = task_dir / "report.md"
        report_bytes = _read_bytes(report_path)
        report_digest = _sha256(report_bytes) if report_bytes else ""
        markers = [
            marker
            for marker in ("done", "fail", "blocked")
            if (task_dir / f".{marker}").is_file()
        ]
        flow_markers = [
            marker
            for marker in FLOW_MARKERS
            if (task_dir / f".{marker}").is_file()
        ]
        terminal_state = "running"
        terminal_marker_age_seconds: float | None = None
        if len(markers) == 1:
            try:
                terminal_marker_age_seconds = max(
                    0.0,
                    time.time() - (task_dir / f".{markers[0]}").stat().st_mtime,
                )
            except OSError:
                terminal_marker_age_seconds = None
        terminal_report_settle_pending = bool(
            len(markers) == 1
            and not report_digest
            and terminal_marker_age_seconds is not None
            and terminal_marker_age_seconds <= TERMINAL_REPORT_SETTLE_SECONDS
        )
        if len(markers) == 1:
            if terminal_report_settle_pending:
                terminal_state = "running"
            elif not report_digest:
                terminal_state = "invalid"
            else:
                terminal_state = {
                    "done": "succeeded",
                    "fail": "failed",
                    "blocked": "blocked",
                }[markers[0]]
        elif len(markers) > 1:
            terminal_state = "invalid"
            health["terminal_conflict"] = True

        attempt_id = str(
            task_metadata.get("attempt_id")
            or fence.get("attempt_id")
            or ""
        )
        contract_digest = _artifact_contract_digest(task_metadata, fence)
        terminal_evidence_digest = ""
        if markers:
            terminal_evidence_digest = _canonical_sha256(
                {
                    "attempt_id": attempt_id,
                    "contract_digest": contract_digest,
                    "markers": markers,
                    "report_digest": report_digest,
                }
            )
        if markers and (
            len(markers) != 1
            or (not report_digest and not terminal_report_settle_pending)
        ):
            health["terminal_conflict"] = True

        lanes[node_id] = {
            "attempt_id": attempt_id,
            "terminal_state": terminal_state,
            "report_digest": report_digest,
            "terminal_evidence_digest": terminal_evidence_digest,
            "task_contract_digest": contract_digest,
            "task_dir": task_dir_value,
            "terminal_marker": markers[0] if len(markers) == 1 else "",
            "terminal_marker_age_seconds": terminal_marker_age_seconds,
            "terminal_report_settle_pending": terminal_report_settle_pending,
            "terminal_report_settle_seconds": TERMINAL_REPORT_SETTLE_SECONDS,
            "flow_markers": flow_markers,
            "has_submitted": "submitted" in flow_markers,
            "has_received": "received" in flow_markers,
            "has_started": "started" in flow_markers,
            "has_heartbeat": "heartbeat" in flow_markers,
        }

    return {
        "schema_version": 1,
        "source": "task_artifacts",
        "run_id": manifest["run_id"],
        "workflow_revision": manifest["workflow_revision"],
        "lanes": lanes,
        "health": health,
        "advisory": {},
        "target_bindings": {},
    }


def supervision_control_root(root: Path | str, run_id: str) -> Path:
    _safe_id(run_id, "run_id")
    return Path(root).resolve() / ".omx" / "framework" / "supervision" / "runs" / run_id


def acquire_actuation_lease(
    root: Path | str,
    *,
    run_id: str,
    supervisor_instance_id: str,
    policy_digest: str,
    build_digest: str,
    ttl_seconds: int,
    now: str | None = None,
) -> dict[str, Any]:
    _safe_id(supervisor_instance_id, "supervisor_instance_id")
    if type(ttl_seconds) is not int or ttl_seconds < 1:
        raise SupervisionControlError("ttl_seconds must be a positive integer")
    timestamp = _timestamp(now)
    control_root = supervision_control_root(root, run_id)
    with _control_lock(control_root):
        current = _read_json(control_root / "lease.json")
        current_epoch = _nonnegative_int(current.get("lease_epoch"))
        current_owner = str(current.get("supervisor_instance_id") or "")
        active = bool(current) and not _expired(current.get("expires_at"), timestamp)
        if active and current_owner != supervisor_instance_id:
            raise SupervisionControlConflict("actuation lease is active")
        if active and (
            str(current.get("policy_digest") or "") != str(policy_digest)
            or str(current.get("build_digest") or "") != str(build_digest)
        ):
            raise SupervisionControlConflict(
                "actuation lease policy/build digest changed"
            )
        epoch = current_epoch if active else current_epoch + 1
        lease = {
            "schema_version": 1,
            "run_id": run_id,
            "supervisor_instance_id": supervisor_instance_id,
            "lease_epoch": epoch,
            "fencing_token": f"{run_id}:{epoch}",
            "policy_digest": str(policy_digest),
            "build_digest": str(build_digest),
            "issued_at": _format_timestamp(timestamp),
            "expires_at": _format_timestamp(timestamp + timedelta(seconds=ttl_seconds)),
        }
        _write_json_atomic(control_root / "lease.json", lease)
        _append_canonical_event(
            control_root,
            "actuation_lease_acquired",
            {"lease_epoch": epoch, "supervisor_instance_id": supervisor_instance_id},
            timestamp,
        )
        return lease


def claim_edge(
    root: Path | str,
    *,
    run_id: str,
    workflow_revision: str,
    edge_id: str,
    logical_action_id: str,
    lease: dict[str, Any],
    now: str | None = None,
    actor_kind: str = "supervisor",
) -> dict[str, Any]:
    _safe_id(workflow_revision, "workflow_revision")
    _safe_id(edge_id, "edge_id")
    _safe_logical_action_id(logical_action_id)
    _safe_id(actor_kind, "actor_kind")
    timestamp = _timestamp(now)
    control_root = supervision_control_root(root, run_id)
    with _control_lock(control_root):
        _require_current_lease(control_root, lease, timestamp)
        path = control_root / "claims" / f"{_edge_key(workflow_revision, edge_id)}.json"
        current = _read_json(path)
        if current:
            if str(current.get("logical_action_id") or "") == logical_action_id:
                return current
            raise SupervisionControlConflict("workflow edge already claimed")
        claim = {
            "schema_version": 1,
            "run_id": run_id,
            "workflow_revision": workflow_revision,
            "edge_id": edge_id,
            "logical_action_id": logical_action_id,
            "actor_kind": actor_kind,
            "state": "reserved",
            "supervisor_instance_id": lease["supervisor_instance_id"],
            "lease_epoch": lease["lease_epoch"],
            "fencing_token": lease["fencing_token"],
            "reserved_at": _format_timestamp(timestamp),
        }
        _write_json_atomic(path, claim)
        _append_canonical_event(
            control_root,
            "edge_reserved",
            {"edge_id": edge_id, "logical_action_id": logical_action_id},
            timestamp,
        )
        return claim


def write_action_intent(
    root: Path | str,
    *,
    run_id: str,
    logical_action_id: str,
    claim: dict[str, Any],
    handoff: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    _safe_logical_action_id(logical_action_id)
    timestamp = _timestamp(now)
    control_root = supervision_control_root(root, run_id)
    with _control_lock(control_root):
        if str(claim.get("logical_action_id") or "") != logical_action_id:
            raise SupervisionControlConflict("claim logical_action_id mismatch")
        path = _action_path(control_root, logical_action_id)
        current = _read_json(path)
        if current:
            if str(current.get("logical_action_id") or "") != logical_action_id:
                raise SupervisionControlConflict("action record logical_action_id mismatch")
            return {**current, "_existing": True}
        action = {
            "schema_version": 1,
            "run_id": run_id,
            "logical_action_id": logical_action_id,
            "edge_id": str(claim.get("edge_id") or ""),
            "workflow_revision": str(claim.get("workflow_revision") or ""),
            "claim": claim,
            "handoff": handoff,
            "state": "intent_durable",
            "intent_written_at": _format_timestamp(timestamp),
            "automatic_resubmit_allowed": False,
        }
        _write_json_atomic(path, action)
        _append_canonical_event(
            control_root,
            "action_intent_durable",
            {"logical_action_id": logical_action_id},
            timestamp,
        )
        return {**action, "_existing": False}


def record_action_receipt(
    root: Path | str,
    *,
    run_id: str,
    logical_action_id: str,
    receipt: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    _safe_logical_action_id(logical_action_id)
    timestamp = _timestamp(now)
    control_root = supervision_control_root(root, run_id)
    with _control_lock(control_root):
        path = _action_path(control_root, logical_action_id)
        action = _read_json(path)
        if not action:
            raise SupervisionControlError("action intent not found")
        updated = {
            **action,
            "state": "accepted",
            "receipt": receipt,
            "receipt_recorded_at": _format_timestamp(timestamp),
            "automatic_resubmit_allowed": False,
        }
        _write_json_atomic(path, updated)
        _append_canonical_event(
            control_root,
            "action_receipt_accepted",
            {"logical_action_id": logical_action_id},
            timestamp,
        )
        return updated


def lookup_action(root: Path | str, *, run_id: str, logical_action_id: str) -> dict[str, Any]:
    _safe_logical_action_id(logical_action_id)
    return _read_json(_action_path(supervision_control_root(root, run_id), logical_action_id))


def validate_dispatch_claim(
    root: Path | str,
    *,
    run_id: str,
    logical_action_id: str,
    fencing_token: str,
    lease_epoch: int,
    now: str | None = None,
) -> dict[str, Any]:
    """Recheck a supervisor intent under its canonical lease before dispatch."""
    _safe_id(run_id, "run_id")
    _safe_logical_action_id(logical_action_id)
    if type(lease_epoch) is not int or lease_epoch < 1:
        raise SupervisionControlConflict("actuation fencing epoch is invalid")
    timestamp = _timestamp(now)
    control_root = supervision_control_root(root, run_id)
    with _control_lock(control_root):
        lease = _read_json(control_root / "lease.json")
        if (
            not lease
            or _expired(lease.get("expires_at"), timestamp)
            or int(lease.get("lease_epoch") or 0) != lease_epoch
            or str(lease.get("fencing_token") or "") != fencing_token
        ):
            raise SupervisionControlConflict("actuation fencing token is stale")
        action = _read_json(_action_path(control_root, logical_action_id))
        claim = action.get("claim") if isinstance(action.get("claim"), dict) else {}
        if (
            str(action.get("run_id") or "") != run_id
            or str(action.get("logical_action_id") or "") != logical_action_id
            or str(action.get("state") or "") != "intent_durable"
            or str(claim.get("logical_action_id") or "") != logical_action_id
            or int(claim.get("lease_epoch") or 0) != lease_epoch
            or str(claim.get("fencing_token") or "") != fencing_token
        ):
            raise SupervisionControlConflict("supervisor action claim is not dispatchable")
        return action


def reconcile_action(
    root: Path | str,
    *,
    run_id: str,
    logical_action_id: str,
    receipt: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    _safe_logical_action_id(logical_action_id)
    if receipt is not None and (
        not isinstance(receipt, dict) or not receipt
    ):
        raise SupervisionControlError("reconciled receipt must be a non-empty object")
    timestamp = _timestamp(now)
    control_root = supervision_control_root(root, run_id)
    with _control_lock(control_root):
        path = _action_path(control_root, logical_action_id)
        action = _read_json(path)
        if not action:
            raise SupervisionControlError("action intent not found")
        if str(action.get("state") or "") == "accepted":
            return action
        if str(action.get("state") or "") != "intent_durable":
            return action
        if receipt is not None:
            reconciled = {
                **action,
                "state": "accepted",
                "receipt": receipt,
                "receipt_recorded_at": _format_timestamp(timestamp),
                "reconciled_at": _format_timestamp(timestamp),
                "automatic_resubmit_allowed": False,
            }
            _write_json_atomic(path, reconciled)
            _append_canonical_event(
                control_root,
                "action_receipt_reconciled",
                {"logical_action_id": logical_action_id},
                timestamp,
            )
            return reconciled
        reconciled = {
            **action,
            "state": "delivery_uncertain",
            "reconciled_at": _format_timestamp(timestamp),
            "automatic_resubmit_allowed": False,
        }
        _write_json_atomic(path, reconciled)
        _append_canonical_event(
            control_root,
            "action_delivery_uncertain",
            {"logical_action_id": logical_action_id},
            timestamp,
        )
        return reconciled


def _require_current_lease(
    control_root: Path,
    lease: dict[str, Any],
    timestamp: datetime,
) -> None:
    current = _read_json(control_root / "lease.json")
    if not current:
        raise SupervisionControlConflict("actuation lease missing")
    if _expired(current.get("expires_at"), timestamp):
        raise SupervisionControlConflict("actuation lease stale")
    for field in ("supervisor_instance_id", "lease_epoch", "fencing_token"):
        if current.get(field) != lease.get(field):
            raise SupervisionControlConflict("actuation lease stale")


@contextmanager
def _control_lock(control_root: Path) -> Iterator[None]:
    control_root.mkdir(parents=True, exist_ok=True)
    lock_path = control_root / "control.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _append_canonical_event(
    control_root: Path,
    event_type: str,
    payload: dict[str, Any],
    timestamp: datetime,
) -> None:
    events_dir = control_root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    sequence = len(list(events_dir.glob("*.json"))) + 1
    event = {
        "schema_version": 1,
        "event_id": hashlib.sha256(
            f"{sequence}:{event_type}:{_format_timestamp(timestamp)}".encode("utf-8")
        ).hexdigest()[:20],
        "sequence": sequence,
        "type": event_type,
        "created_at": _format_timestamp(timestamp),
        "payload": payload,
    }
    _write_json_atomic(events_dir / f"{sequence:08d}-{event['event_id']}.json", event)


def _action_path(control_root: Path, logical_action_id: str) -> Path:
    return control_root / "actions" / f"{_logical_filename(logical_action_id)}.json"


def _edge_key(workflow_revision: str, edge_id: str) -> str:
    return hashlib.sha256(f"{workflow_revision}:{edge_id}".encode("utf-8")).hexdigest()


def _edge_health_path(
    root: Path,
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    decision: dict[str, Any],
) -> Path | None:
    edge_id = str(decision.get("selected_edge_id") or "")
    effect_class = str(decision.get("effect_class") or "")
    profile_id = str(profile.get("profile_id") or "")
    workflow_revision = str(manifest.get("workflow_revision") or "")
    if not edge_id or not effect_class or not profile_id or not workflow_revision:
        return None
    key = hashlib.sha256(
        f"{profile_id}:{workflow_revision}:{edge_id}:{effect_class}".encode(
            "utf-8"
        )
    ).hexdigest()
    return (
        root
        / ".omx"
        / "framework"
        / "supervision"
        / "edge-health"
        / f"{key}.json"
    )


def _edge_circuit_open(
    root: Path,
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    decision: dict[str, Any],
) -> bool:
    path = _edge_health_path(
        root,
        profile=profile,
        manifest=manifest,
        decision=decision,
    )
    if path is None:
        return False
    with _control_lock(path.parent):
        health = _read_edge_health(path)
    return bool(
        health.get("corrupt")
        or int(health.get("consecutive_delivery_uncertain") or 0)
        >= EDGE_CIRCUIT_FAILURE_THRESHOLD
    )


def _tracks_edge_health(decision: dict[str, Any]) -> bool:
    return (
        decision.get("reason")
        in {"delivery_uncertain", "edge_circuit_open"}
        or decision.get("executed_action") in {"accepted", "already_accepted"}
    )


def _record_edge_health(
    root: Path,
    *,
    profile: dict[str, Any],
    manifest: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    path = _edge_health_path(
        root,
        profile=profile,
        manifest=manifest,
        decision=decision,
    )
    if path is None:
        return {}
    with _control_lock(path.parent):
        previous = _read_edge_health(path)
        if previous.get("corrupt"):
            return previous
        failures = int(previous.get("consecutive_delivery_uncertain") or 0)
        if decision.get("reason") == "delivery_uncertain":
            failures += 1
        elif decision.get("executed_action") in {
            "accepted",
            "already_accepted",
        }:
            failures = 0
        health = {
            "schema_version": 1,
            "profile_id": str(profile.get("profile_id") or ""),
            "workflow_revision": str(manifest.get("workflow_revision") or ""),
            "edge_id": str(decision.get("selected_edge_id") or ""),
            "effect_class": str(decision.get("effect_class") or ""),
            "consecutive_delivery_uncertain": failures,
            "circuit_open": failures >= EDGE_CIRCUIT_FAILURE_THRESHOLD,
            "last_reason": str(decision.get("reason") or ""),
            "updated_at": _format_timestamp(datetime.now(timezone.utc)),
        }
        _write_json_atomic(path, health)
        return health


def _read_edge_health(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"corrupt": True}
    return payload if isinstance(payload, dict) else {"corrupt": True}


def _logical_filename(logical_action_id: str) -> str:
    return logical_action_id.split(":", 1)[-1]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prepare_output_dir(
    output_dir: Path | str,
    *,
    identity: dict[str, str],
    resume: bool,
) -> Path:
    output = Path(output_dir).expanduser()
    if output.exists() and output.is_symlink():
        raise SupervisionRuntimeError("supervisor output_dir must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    identity_path = output / "identity.json"
    nonempty = any(output.iterdir())
    if identity_path.is_symlink():
        raise SupervisionRuntimeError("supervisor output identity must not be a symlink")
    if identity_path.exists():
        existing = _read_json(identity_path)
        if not existing:
            raise SupervisionRuntimeError("supervisor output identity is invalid")
        if existing != identity:
            raise SupervisionRuntimeError("supervisor output identity mismatch")
        if not resume and nonempty:
            raise SupervisionRuntimeError("supervisor output_dir already exists; use --resume")
    elif nonempty:
        raise SupervisionRuntimeError("supervisor output identity is missing")
    else:
        _write_json_atomic(identity_path, identity)
    for directory in ("snapshots", "decisions", "notifications", "proposals"):
        (output / directory).mkdir(exist_ok=True)
    return output


def _write_cycle_artifacts(
    output: Path,
    cycle: int,
    snapshot: dict[str, Any],
    decision: dict[str, Any],
) -> bool:
    _write_json_atomic(output / "snapshots" / f"{cycle:08d}.json", snapshot)
    decision_id = str(decision.get("decision_id") or f"cycle-{cycle:08d}")
    safe_name = _safe_filename(decision_id)
    _write_json_atomic(output / "decisions" / f"{safe_name}.json", decision)
    event = {
        "schema_version": 1,
        "sequence": cycle,
        "type": "supervisor_decision",
        "decision_id": str(decision.get("decision_id") or ""),
        "disposition": str(decision.get("disposition") or ""),
        "reason": str(decision.get("reason") or ""),
    }
    _append_projection_event(output / "events.jsonl", event)
    if decision.get("reason") == "replan_required":
        _write_replan_request(output, snapshot, decision)
    if decision.get("disposition") == "escalate":
        incident = hashlib.sha256(
            (
                f"{decision.get('reason')}:{decision.get('selected_edge_id')}:"
                f"{decision.get('evidence_snapshot_digest')}"
            ).encode("utf-8")
        ).hexdigest()
        path = output / "notifications" / f"{incident}.json"
        if not path.exists():
            _write_json_atomic(
                path,
                {
                    "schema_version": 1,
                    "incident_id": incident,
                    "reason": decision.get("reason"),
                    "decision_id": decision.get("decision_id"),
                    "recommended_action": "leader_review_required",
                },
            )
            return True
    return False


def _write_replan_request(
    output: Path,
    snapshot: dict[str, Any],
    decision: dict[str, Any],
) -> Path:
    digest = str(decision.get("evidence_snapshot_digest") or "")
    filename = _safe_filename(digest.removeprefix("sha256:") or "replan")
    path = output / "proposals" / f"replan-{filename}.json"
    if not path.exists():
        _write_json_atomic(
            path,
            {
                "schema_version": 1,
                "kind": "replan_request",
                "effect": "proposal",
                "approved": False,
                "run_id": snapshot.get("run_id"),
                "workflow_revision": snapshot.get("workflow_revision"),
                "decision_id": decision.get("decision_id"),
                "evidence_snapshot_digest": digest,
                "lane_states": decision.get("lane_states"),
            },
        )
    return path


def _write_local_proposal(
    output: Path,
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    decision: dict[str, Any],
) -> Path:
    logical_action = str(decision.get("logical_action_id") or "")
    filename = _safe_filename(logical_action or str(decision.get("decision_id") or "proposal"))
    path = output / "proposals" / f"{filename}.json"
    _write_json_atomic(
        path,
        {
            "schema_version": 1,
            "effect": "proposal",
            "approved": False,
            "run_id": manifest["run_id"],
            "workflow_revision": manifest["workflow_revision"],
            "logical_action_id": logical_action,
            "decision_id": decision.get("decision_id"),
            "selected_edge_id": decision.get("selected_edge_id"),
            "evidence_snapshot": snapshot,
        },
    )
    return path


def _structured_handoff(
    manifest: dict[str, Any],
    decision: dict[str, Any],
    snapshot: dict[str, Any],
    claim: dict[str, Any],
) -> dict[str, Any]:
    target_node = str(decision.get("target_node") or "")
    targets = manifest.get("targets") if isinstance(manifest.get("targets"), dict) else {}
    task_template = targets.get(target_node) if isinstance(targets.get(target_node), dict) else {}
    target_bindings = (
        snapshot.get("target_bindings")
        if isinstance(snapshot.get("target_bindings"), dict)
        else {}
    )
    observed_binding = (
        target_bindings.get(target_node)
        if isinstance(target_bindings.get(target_node), dict)
        else {}
    )
    target = {
        "node_id": target_node,
        "worker_id": task_template.get("worker_id"),
        "task_title": task_template.get("task_title"),
        "task_body": task_template.get("task_body"),
        "session_context_requirement": task_template.get(
            "session_context_requirement"
        ),
        "expected_binding": {
            "instance_id": observed_binding.get("instance_id"),
            "binding_generation": observed_binding.get("binding_generation"),
            "fencing_generation": observed_binding.get("fencing_generation"),
        },
    }
    if decision.get("effect_class") == "write_isolated":
        target["isolated_write"] = task_template.get("isolated_write")
    return {
        "schema_version": 1,
        "run_id": manifest["run_id"],
        "workflow_revision": manifest["workflow_revision"],
        "logical_action_id": decision.get("logical_action_id"),
        "source_attempts": [
            {
                "node_id": lane.get("node_id"),
                "attempt_id": lane.get("attempt_id"),
                "task_contract_digest": lane.get("task_contract_digest"),
            }
            for lane in manifest.get("lanes", [])
            if isinstance(lane, dict)
        ],
        "evidence_snapshot_digest": decision.get("evidence_snapshot_digest"),
        "edge_id": decision.get("selected_edge_id"),
        "effect_class": decision.get("effect_class"),
        "supervision": {
            "run_id": claim.get("run_id"),
            "lease_epoch": claim.get("lease_epoch"),
            "fencing_token": claim.get("fencing_token"),
        },
        "target": target,
    }


def _is_local_effect(effect_class: str) -> bool:
    return effect_class in {"artifact_only", "local_no_live", "observe"}


def _is_worker_effect(effect_class: str) -> bool:
    return effect_class in {"read_only_worker", "write_isolated", "shared_write"}


def _append_projection_event(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _write_summary(output: Path, result: dict[str, Any]) -> None:
    decision = result.get("last_decision") if isinstance(result.get("last_decision"), dict) else {}
    text = "\n".join(
        [
            "# Supervisor Summary",
            "",
            f"- run_id: {result.get('run_id')}",
            f"- workflow_revision: {result.get('workflow_revision')}",
            f"- mode: {result.get('mode')}",
            f"- cycles: {result.get('cycles')}",
            f"- disposition: {decision.get('disposition') or '-'}",
            f"- reason: {decision.get('reason') or '-'}",
            f"- proposals: {result.get('metrics', {}).get('proposals', 0)}",
            f"- accepted_actions: {result.get('metrics', {}).get('accepted_actions', 0)}",
            f"- escalations: {result.get('metrics', {}).get('escalations', 0)}",
            "",
        ]
    )
    path = output / "summary.md"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _metrics(
    decisions: list[dict[str, Any]],
    notifications_emitted: int,
) -> dict[str, int | float]:
    return {
        "proposals": sum(
            1 for decision in decisions if decision.get("disposition") == "propose"
        ),
        "accepted_actions": sum(
            1 for decision in decisions if decision.get("executed_action") == "accepted"
        ),
        "duplicate_action_suppressions": sum(
            1
            for decision in decisions
            if decision.get("executed_action") == "already_accepted"
        ),
        "delivery_uncertain": sum(
            1
            for decision in decisions
            if decision.get("reason") == "delivery_uncertain"
        ),
        "escalations": sum(
            1 for decision in decisions if decision.get("disposition") == "escalate"
        ),
        "notifications_emitted": notifications_emitted,
        "decision_latency_ms_total": round(
            sum(float(decision.get("decision_latency_ms") or 0) for decision in decisions),
            3,
        ),
        "decision_latency_ms_max": max(
            (float(decision.get("decision_latency_ms") or 0) for decision in decisions),
            default=0.0,
        ),
    }


def _elapsed_ms(started_at: float, finished_at: float) -> float:
    return round(max(0.0, (finished_at - started_at) * 1000), 3)


def _budget_exhausted_decision(
    policy: dict[str, Any],
    *,
    lease_epoch: int,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "disposition": "escalate",
        "reason": "budget_exhausted",
        "selected_edge_id": "",
        "edge_states": {},
        "lane_states": {},
        "run_health": "budget_exhausted",
        "evidence_snapshot_digest": _canonical_sha256(snapshot),
        "policy_digest": supervision_policy_digest(policy),
        "supervisor_lease_epoch": lease_epoch,
        "logical_action_id": "",
        "decision_id": "",
        "effect_class": "",
        "source_nodes": [],
        "target_node": "",
        "would_act": False,
    }


def _build_digest() -> str:
    material = Path(__file__).read_bytes()
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise SupervisionControlError(f"{field} is invalid")
    return text


def _declared_task_dir(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise SupervisionRuntimeError("declared task_dir is unsafe")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SupervisionRuntimeError("declared task_dir escapes root") from exc
    return path


def _artifact_contract_digest(
    task_metadata: dict[str, Any],
    acceptance_fence: dict[str, Any],
) -> str:
    value = str(
        task_metadata.get("task_contract_digest")
        or task_metadata.get("contract_hash")
        or acceptance_fence.get("contract_hash")
        or ""
    )
    if value.startswith("sha256:") and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return f"sha256:{value}"
    return ""


def _read_bytes(path: Path, *, limit: int = 2_000_000) -> bytes:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return b""
        return path.read_bytes()
    except OSError:
        return b""


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_sha256(value: dict[str, Any]) -> str:
    return _sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _safe_logical_action_id(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise SupervisionControlError("logical_action_id must be a sha256 digest")
    return text


def _safe_filename(value: str) -> str:
    if value.startswith("sha256:"):
        value = value.split(":", 1)[1]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return value


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _timestamp(value: str | None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SupervisionControlError("timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise SupervisionControlError("timestamp must include timezone")
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _expired(value: object, timestamp: datetime) -> bool:
    try:
        return _timestamp(str(value)) <= timestamp
    except SupervisionControlError:
        return True


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
