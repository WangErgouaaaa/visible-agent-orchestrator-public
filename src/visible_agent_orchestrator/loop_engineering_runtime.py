"""Artifact-only runtime for bounded loop-engineering observation and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.loop_engineering import (
    build_advisory_review,
    LoopEngineeringError,
    build_main_adoption_request,
    build_main_command_result,
    reduce_thinker_feedback,
    reduce_watchdog_cycle,
    validate_main_adoption_decision,
    validate_main_command,
)
from visible_agent_orchestrator.terminal_acceptance import (
    acceptance_fence_hash,
    terminal_acceptance_status,
)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LoopEngineeringRuntimeError(RuntimeError):
    """Raised when local loop-engineering artifact lifecycle is invalid."""


def build_worker_health_snapshot(
    *,
    contract: Any,
    run_id: str,
    workflow_revision: str,
    lane_id: str,
    attempt_id: str,
    worker_id: str,
) -> dict[str, Any]:
    """Normalize sanctioned worker-scoped checks into one no-live snapshot."""
    for label, value in (
        ("run_id", run_id),
        ("workflow_revision", workflow_revision),
        ("lane_id", lane_id),
        ("attempt_id", attempt_id),
        ("worker_id", worker_id),
    ):
        if not SAFE_ID.fullmatch(value):
            raise LoopEngineeringRuntimeError(f"{label} is invalid")

    doctor = _mapping(contract.doctor(worker_id))
    health_check = _mapping(contract.health(worker_id))
    rebind = _mapping(contract.rebind_check(worker_id))
    locks = _mapping(contract.locks(worker_id))
    doctor_parsed = _mapping(doctor.get("parsed"))
    health_parsed = _mapping(health_check.get("parsed"))
    rebind_parsed = _mapping(rebind.get("parsed"))
    locks_parsed = _mapping(locks.get("parsed"))

    binding_status = str(
        rebind_parsed.get("status")
        or doctor_parsed.get("rebind_status")
        or health_parsed.get("status")
        or ""
    )
    binding_confidence = str(
        rebind_parsed.get("binding_confidence")
        or doctor_parsed.get("binding_confidence")
        or ""
    )
    try:
        lock_count = int(locks_parsed.get("target_worker_lock_count"))
    except (TypeError, ValueError) as exc:
        raise LoopEngineeringRuntimeError(
            "target_worker_lock_count is invalid"
        ) from exc
    if lock_count < 0:
        raise LoopEngineeringRuntimeError("target_worker_lock_count is invalid")
    health: dict[str, bool] = {}
    if binding_status == "port_not_live" or str(
        health_parsed.get("status") or ""
    ) == "port_not_live":
        health["connection_lost"] = True
    elif (
        binding_status != "current_binding_ok"
        or binding_confidence != "strong"
        or not bool(doctor.get("ok"))
        or not bool(health_check.get("ok"))
        or not bool(rebind.get("ok"))
    ):
        health["binding_drift"] = True

    return {
        "schema_version": 1,
        "run_id": run_id,
        "workflow_revision": workflow_revision,
        "lanes": {
            lane_id: {
                "attempt_id": attempt_id,
                "terminal_state": "running",
                "progress_token": "",
            }
        },
        "target_bindings": {
            lane_id: {
                "worker_id": worker_id,
                "binding_status": binding_status,
                "binding_confidence": binding_confidence,
                "target_worker_lock_count": lock_count,
            }
        },
        "health": health,
    }


def observe_worker_once(
    *,
    contract: Any,
    profile: dict[str, Any],
    output_dir: Path | str,
    run_id: str,
    workflow_revision: str,
    lane_id: str,
    attempt_id: str,
    worker_id: str,
    stale_cycle_threshold: int,
) -> dict[str, Any]:
    """Observe one worker through sanctioned read-only control-plane checks."""
    snapshot = build_worker_health_snapshot(
        contract=contract,
        run_id=run_id,
        workflow_revision=workflow_revision,
        lane_id=lane_id,
        attempt_id=attempt_id,
        worker_id=worker_id,
    )
    result = run_loop_watchdog(
        profile=profile,
        snapshots=[snapshot],
        output_dir=output_dir,
        stale_cycle_threshold=stale_cycle_threshold,
        max_cycles=1,
    )
    return {
        **result,
        "snapshot": snapshot,
    }


def run_loop_watchdog(
    *,
    profile: dict[str, Any],
    snapshots: list[dict[str, Any]],
    output_dir: Path | str,
    stale_cycle_threshold: int,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Observe a bounded snapshot sequence and write only local artifacts."""
    if not isinstance(snapshots, list) or not snapshots:
        raise LoopEngineeringRuntimeError("watchdog requires snapshots")
    if max_cycles is not None and (
        type(max_cycles) is not int or max_cycles < 1
    ):
        raise LoopEngineeringRuntimeError("max_cycles must be positive")
    output = _prepare_output_dir(output_dir)
    previous = _read_json(output / "watchdog_state.json")
    completed_cycles = _completed_cycles(previous)
    notification_state = _read_json(output / "notification_state.json")
    notification_fingerprints = _notification_fingerprints(
        notification_state,
        completed_cycles=completed_cycles,
    )
    _validate_cycle_ledger(output / "cycles", completed_cycles)
    limit = min(len(snapshots), max_cycles or len(snapshots))
    incidents: list[dict[str, Any]] = []
    emitted = 0
    duplicates = 0
    notifications_emitted = 0
    notifications_suppressed = 0
    cycles: list[dict[str, Any]] = []
    for offset, snapshot in enumerate(snapshots[:limit], start=1):
        try:
            cycle = reduce_watchdog_cycle(
                profile=profile,
                snapshot=snapshot,
                previous=previous,
                stale_cycle_threshold=stale_cycle_threshold,
            )
        except LoopEngineeringError as exc:
            raise LoopEngineeringRuntimeError(str(exc)) from exc
        sequence = completed_cycles + offset
        cycle_path = output / "cycles" / f"{sequence:08d}.json"
        if cycle_path.exists():
            raise LoopEngineeringRuntimeError(
                f"cycle record already exists: {cycle_path.name}"
            )
        _write_json_atomic(cycle_path, cycle)
        incident = cycle.get("incident")
        if isinstance(incident, dict):
            incident_id = str(incident.get("incident_id") or "")
            if not SAFE_ID.fullmatch(incident_id):
                raise LoopEngineeringRuntimeError("incident id is invalid")
            path = output / "inbox" / f"{incident_id}.json"
            if path.exists():
                current = _read_json(path)
                if current != incident:
                    raise LoopEngineeringRuntimeError("incident identity collision")
                duplicates += 1
            else:
                _write_json_atomic(path, incident)
                incidents.append(incident)
                emitted += 1
            if _write_notification_if_due(
                output=output,
                incident=incident,
                sequence=sequence,
                fingerprints=notification_fingerprints,
            ):
                notifications_emitted += 1
            else:
                notifications_suppressed += 1
        previous = {
            "progress_digest": cycle["progress_digest"],
            "stale_cycles": cycle["stale_cycles"],
            "state": cycle["state"],
            "evidence_snapshot_digest": cycle["evidence_snapshot_digest"],
            "completed_cycles": sequence,
        }
        cycles.append(cycle)
    result = {
        "schema_version": 1,
        "loop_kind": "artifact_only_watchdog",
        "cycles": len(cycles),
        "cycle_sequence_start": completed_cycles + 1,
        "cycle_sequence_end": completed_cycles + len(cycles),
        "last_cycle": cycles[-1],
        "incidents": incidents,
        "incidents_emitted": emitted,
        "duplicate_incidents_suppressed": duplicates,
        "notifications_emitted": notifications_emitted,
        "notifications_suppressed": notifications_suppressed,
        "worker_effects_executed": 0,
        "output_dir": str(output),
    }
    _write_json_atomic(output / "watchdog_state.json", previous)
    _write_json_atomic(
        output / "notification_state.json",
        {
            "schema_version": 1,
            "fingerprints": notification_fingerprints,
        },
    )
    _write_json_atomic(output / "watchdog_result.json", result)
    return result


def execute_main_command(
    *,
    output_dir: Path | str,
    command: dict[str, Any],
) -> dict[str, Any]:
    """Record one Main-owned, artifact-only recovery decision idempotently."""
    output = _prepare_output_dir(output_dir)
    command_id = str(command.get("command_id") or "")
    if not SAFE_ID.fullmatch(command_id):
        raise LoopEngineeringRuntimeError("command_id is invalid")
    result_path = output / "command-results" / f"{command_id}.json"
    if result_path.exists():
        prior = _read_json(result_path)
        if _read_json(output / "commands" / f"{command_id}.json") != command:
            raise LoopEngineeringRuntimeError("command id already has different content")
        return {**prior, "status": "already_processed"}
    incident_id = str(command.get("incident_id") or "")
    if not SAFE_ID.fullmatch(incident_id):
        raise LoopEngineeringRuntimeError("incident_id is invalid")
    incident = _read_json(output / "inbox" / f"{incident_id}.json")
    if not incident:
        raise LoopEngineeringRuntimeError("incident does not exist")
    try:
        normalized = validate_main_command(command=command, incident=incident)
        result = build_main_command_result(command=normalized, incident=incident)
    except LoopEngineeringError as exc:
        raise LoopEngineeringRuntimeError(str(exc)) from exc
    packet = result.get("leader_packet")
    if isinstance(packet, dict):
        _write_json_atomic(output / "leader-packets" / f"{command_id}.json", packet)
    _write_json_atomic(output / "commands" / f"{command_id}.json", command)
    _write_json_atomic(result_path, result)
    return result


def record_thinker_feedback(
    *,
    output_dir: Path | str,
    review_bundle: dict[str, Any],
    current_snapshot_digest: str,
    workflow_revision: str,
    max_rounds: int,
    parent_task_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Persist advisory Thinker feedback and an optional Main-only request."""
    if not isinstance(review_bundle, dict):
        raise LoopEngineeringRuntimeError("review_bundle must be an object")
    parent_task_terminal = (
        _parent_task_terminal_binding(parent_task_dir)
        if parent_task_dir is not None
        else None
    )
    output = _prepare_output_dir(output_dir)
    feedback_id = _feedback_id(
        review_bundle=review_bundle,
        current_snapshot_digest=current_snapshot_digest,
        workflow_revision=workflow_revision,
    )
    feedback_path = output / "thinker-feedback" / f"{feedback_id}.json"
    if feedback_path.exists():
        prior = _read_json(feedback_path)
        if str(prior.get("feedback_id") or "") != feedback_id:
            raise LoopEngineeringRuntimeError("feedback artifact identity collision")
        advisory_review = _mapping_or_none(prior.get("advisory_review"))
        if advisory_review is None:
            raise LoopEngineeringRuntimeError("feedback artifact missing advisory review")
        if (
            parent_task_terminal is not None
            and advisory_review.get("parent_task_terminal") != parent_task_terminal
        ):
            raise LoopEngineeringRuntimeError(
                "feedback artifact parent task terminal does not match"
            )
        return {
            "schema_version": 1,
            "status": "already_processed",
            "feedback": _mapping(prior.get("feedback")),
            "advisory_review": advisory_review,
            "adoption_request": _mapping_or_none(
                prior.get("adoption_request")
            ),
            "worker_effects_executed": 0,
        }

    previous = _read_json(output / "thinker_feedback_state.json")
    try:
        feedback = reduce_thinker_feedback(
            review_bundle=review_bundle,
            current_snapshot_digest=current_snapshot_digest,
            workflow_revision=workflow_revision,
            previous=previous,
            max_rounds=max_rounds,
        )
        adoption_request = build_main_adoption_request(
            feedback_id=feedback_id,
            feedback=feedback,
            review_bundle=review_bundle,
        )
        advisory_review = build_advisory_review(
            feedback_id=feedback_id,
            feedback=feedback,
            review_bundle=review_bundle,
        )
    except LoopEngineeringError as exc:
        raise LoopEngineeringRuntimeError(str(exc)) from exc
    if parent_task_terminal is not None:
        advisory_review = {
            **advisory_review,
            "parent_task_terminal": parent_task_terminal,
        }
    artifact = {
        "schema_version": 1,
        "feedback_id": feedback_id,
        "review_bundle": review_bundle,
        "current_snapshot_digest": current_snapshot_digest,
        "workflow_revision": workflow_revision,
        "feedback": feedback,
        "advisory_review": advisory_review,
        "adoption_request": adoption_request,
        "worker_effects_executed": 0,
    }
    _write_json_atomic(
        output / "advisory-reviews" / f"{feedback_id}.json",
        advisory_review,
    )
    if adoption_request is not None:
        _write_json_atomic(
            output / "adoption-requests" / f"{feedback_id}.json",
            adoption_request,
        )
    _write_json_atomic(feedback_path, artifact)
    _write_json_atomic(
        output / "thinker_feedback_state.json",
        {
            **feedback,
            "feedback_id": feedback_id,
        },
    )
    return {
        "schema_version": 1,
        "status": "recorded",
        "feedback": feedback,
        "advisory_review": advisory_review,
        "adoption_request": adoption_request,
        "worker_effects_executed": 0,
    }


def record_main_adoption(
    *,
    output_dir: Path | str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Persist one Main-owned adoption result without changing any worker state."""
    output = _prepare_output_dir(output_dir)
    if not isinstance(decision, dict):
        raise LoopEngineeringRuntimeError("main adoption decision must be an object")
    decision_id = str(decision.get("decision_id") or "")
    feedback_id = str(decision.get("feedback_id") or "")
    if not SAFE_ID.fullmatch(decision_id):
        raise LoopEngineeringRuntimeError("decision_id is invalid")
    if not SAFE_ID.fullmatch(feedback_id):
        raise LoopEngineeringRuntimeError("feedback_id is invalid")
    adoption_request = _read_json(output / "adoption-requests" / f"{feedback_id}.json")
    if not adoption_request:
        raise LoopEngineeringRuntimeError("adoption request does not exist")
    try:
        normalized = validate_main_adoption_decision(
            decision=decision,
            adoption_request=adoption_request,
        )
    except LoopEngineeringError as exc:
        raise LoopEngineeringRuntimeError(str(exc)) from exc

    result_path = output / "adoption-decisions" / f"{decision_id}.json"
    if result_path.exists():
        prior = _read_json(result_path)
        if prior != normalized:
            raise LoopEngineeringRuntimeError(
                "adoption decision id already has different content"
            )
        return {
            "schema_version": 1,
            "status": "already_processed",
            "adoption": prior,
            "worker_effects_executed": 0,
        }
    _write_json_atomic(result_path, normalized)
    return {
        "schema_version": 1,
        "status": "recorded",
        "adoption": normalized,
        "worker_effects_executed": 0,
    }


def _prepare_output_dir(output_dir: Path | str) -> Path:
    output = Path(output_dir).expanduser()
    if output.is_symlink():
        raise LoopEngineeringRuntimeError("output_dir must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    for name in (
        "cycles",
        "inbox",
        "commands",
        "command-results",
        "leader-packets",
        "notifications",
        "thinker-feedback",
        "advisory-reviews",
        "adoption-requests",
        "adoption-decisions",
    ):
        artifact_dir = output / name
        if artifact_dir.is_symlink():
            raise LoopEngineeringRuntimeError(
                "artifact directory must not be a symlink"
            )
        if artifact_dir.exists() and not artifact_dir.is_dir():
            raise LoopEngineeringRuntimeError(
                "artifact directory path must be a directory"
            )
        artifact_dir.mkdir(exist_ok=True)
    return output


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise LoopEngineeringRuntimeError(f"JSON artifact must not be a symlink: {path}")
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoopEngineeringRuntimeError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise LoopEngineeringRuntimeError(f"JSON artifact must be an object: {path}")
    return value


def _completed_cycles(state: dict[str, Any]) -> int:
    value = state.get("completed_cycles")
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise LoopEngineeringRuntimeError(
            "watchdog_state completed_cycles must be a non-negative integer"
        )
    return value


def _validate_cycle_ledger(cycles_dir: Path, completed_cycles: int) -> None:
    sequences: list[int] = []
    for path in cycles_dir.iterdir():
        if path.name.startswith("."):
            continue
        if path.is_symlink():
            raise LoopEngineeringRuntimeError(
                "cycle ledger contains a symlink"
            )
        if not path.is_file() or not re.fullmatch(r"\d{8}\.json", path.name):
            raise LoopEngineeringRuntimeError(
                "cycle ledger contains an invalid artifact"
            )
        sequences.append(int(path.stem))
    if sorted(sequences) != list(range(1, completed_cycles + 1)):
        raise LoopEngineeringRuntimeError(
            "cycle ledger does not match watchdog_state"
        )


def _write_notification_if_due(
    *,
    output: Path,
    incident: dict[str, Any],
    sequence: int,
    fingerprints: dict[str, Any],
) -> bool:
    fingerprint = str(incident.get("notification_fingerprint") or "")
    if not SAFE_ID.fullmatch(fingerprint):
        raise LoopEngineeringRuntimeError("notification fingerprint is invalid")
    cooldown = incident.get("notification_cooldown_cycles")
    if type(cooldown) is not int or cooldown < 0:
        raise LoopEngineeringRuntimeError("notification cooldown is invalid")
    previous = _mapping(fingerprints.get(fingerprint))
    last_sequence = previous.get("last_notified_cycle")
    if last_sequence is not None:
        if type(last_sequence) is not int or last_sequence < 1:
            raise LoopEngineeringRuntimeError(
                "notification state cycle is invalid"
            )
        if sequence - last_sequence <= cooldown:
            return False
    notification = {
        "schema_version": 1,
        "cycle_sequence": sequence,
        "notification_fingerprint": fingerprint,
        "incident_id": str(incident.get("incident_id") or ""),
        "kind": str(incident.get("kind") or ""),
        "evidence_snapshot_digest": str(
            incident.get("evidence_snapshot_digest") or ""
        ),
        "requires_main_decision": True,
        "worker_effects_executed": 0,
    }
    _write_json_atomic(
        output / "notifications" / f"{sequence:08d}-{fingerprint}.json",
        notification,
    )
    fingerprints[fingerprint] = {
        "last_notified_cycle": sequence,
        "last_incident_id": notification["incident_id"],
        "last_evidence_snapshot_digest": notification[
            "evidence_snapshot_digest"
        ],
    }
    return True


def _notification_fingerprints(
    state: dict[str, Any],
    *,
    completed_cycles: int,
) -> dict[str, Any]:
    fingerprints = state.get("fingerprints", {})
    if not isinstance(fingerprints, dict):
        raise LoopEngineeringRuntimeError(
            "notification fingerprints are invalid"
        )
    for fingerprint, record in fingerprints.items():
        if not SAFE_ID.fullmatch(str(fingerprint)):
            raise LoopEngineeringRuntimeError(
                "notification fingerprint is invalid"
            )
        last_sequence = _mapping(record).get("last_notified_cycle")
        if type(last_sequence) is not int or last_sequence < 1:
            raise LoopEngineeringRuntimeError(
                "notification state cycle is invalid"
            )
        if last_sequence > completed_cycles:
            raise LoopEngineeringRuntimeError(
                "notification state cycle is ahead"
            )
    return fingerprints


def _feedback_id(
    *,
    review_bundle: dict[str, Any],
    current_snapshot_digest: str,
    workflow_revision: str,
) -> str:
    material = json.dumps(
        {
            "review_bundle": review_bundle,
            "current_snapshot_digest": current_snapshot_digest,
            "workflow_revision": workflow_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _parent_task_terminal_binding(parent_task_dir: Path | str) -> dict[str, Any]:
    task_dir_value = str(parent_task_dir).strip()
    if not task_dir_value:
        raise LoopEngineeringRuntimeError("parent task dir is invalid")
    task_dir = Path(task_dir_value).expanduser()
    if task_dir.is_symlink() or not task_dir.is_dir():
        raise LoopEngineeringRuntimeError("parent task dir is invalid")
    task_dir = task_dir.resolve()
    report_path = task_dir / "report.md"
    if report_path.is_symlink() or not report_path.is_file():
        raise LoopEngineeringRuntimeError("parent task report is missing or empty")
    try:
        report = report_path.read_bytes()
    except OSError as exc:
        raise LoopEngineeringRuntimeError(
            "parent task report is missing or empty"
        ) from exc
    if not report:
        raise LoopEngineeringRuntimeError("parent task report is missing or empty")

    markers: list[str] = []
    for marker in ("done", "fail", "blocked"):
        marker_path = task_dir / f".{marker}"
        if marker_path.is_symlink():
            raise LoopEngineeringRuntimeError(
                "parent task terminal marker must not be a symlink"
            )
        if marker_path.is_file():
            markers.append(marker)
    if not markers:
        raise LoopEngineeringRuntimeError("parent task terminal marker is missing")
    if len(markers) != 1:
        raise LoopEngineeringRuntimeError("parent task terminal markers conflict")
    if markers[0] != "done":
        raise LoopEngineeringRuntimeError("parent task must be completed with .done")

    acceptance = terminal_acceptance_status(task_dir)
    if not bool(acceptance.get("accepted")):
        raise LoopEngineeringRuntimeError("parent task terminal acceptance is invalid")
    acceptance_required = bool(acceptance.get("required"))
    fence_hash = (
        "sha256:" + acceptance_fence_hash(_mapping(acceptance.get("fence")))
        if acceptance_required
        else ""
    )
    report_digest = _sha256_digest(report)
    terminal_evidence_digest = _canonical_digest(
        {
            "terminal_marker": markers[0],
            "report_digest": report_digest,
            "acceptance_fence_hash": fence_hash,
        }
    )
    return {
        "schema_version": 1,
        "terminal_marker": markers[0],
        "terminal_state": "succeeded",
        "report_digest": report_digest,
        "acceptance_fence_required": acceptance_required,
        "acceptance_fence_hash": fence_hash,
        "terminal_evidence_digest": terminal_evidence_digest,
    }


def _sha256_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: dict[str, Any]) -> str:
    return _sha256_digest(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


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
