"""Terminal-output acceptance fencing for visible worker tasks."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.terminal_protocol import (
    TERMINAL_PROTOCOL_CLASSIFICATION_INVALID,
    TERMINAL_PROTOCOL_CLASSIFICATION_LEGACY,
    TERMINAL_PROTOCOL_CLASSIFICATION_MODERN,
    TERMINAL_PROTOCOL_VERSION,
    classify_terminal_protocol,
)
from visible_agent_orchestrator.terminal_protocol_baseline import (
    _read_json_record,
)

TERMINAL_REPORT_SETTLE_SECONDS = 5
FLOW_MARKERS = ("submitted", "received", "started", "heartbeat")
ACCEPTANCE_FIELDS = (
    "worker_id",
    "session_id",
    "task_id",
    "attempt_id",
    "contract_hash",
    "instance_id",
    "binding_generation",
    "fencing_generation",
)
OPERATOR_RECOVERY_CLAIM_KIND = "visible_operator_recovery_closeout"
OPERATOR_RECOVERY_AUTHORIZATION_KIND = (
    "visible_operator_recovery_closeout"
)
OPERATOR_RECOVERY_AUTHORIZATION_FILE = (
    "recovery_closeout_authorization.json"
)
OPERATOR_RECOVERY_CONSUMPTION_FILE = (
    "recovery_closeout_consumption.json"
)
QUARANTINE_REBOOT_CLOSEOUT_BASIS = "reboot_stale_owner_instance_gone"
RECOVERY_ACTIVITY_FILES = (
    ".submitted",
    ".received",
    ".started",
    ".heartbeat",
    "protocol_status.json",
    "events.jsonl",
)


def read_json_object(path: Path) -> dict[str, Any]:
    present, payload, malformed = _read_json_record(path)
    if not present or malformed:
        return {}
    return payload


def acceptance_fence_hash(fence: dict[str, Any]) -> str:
    normalized = normalize_acceptance_identity(fence)
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_acceptance_identity(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_id": str(payload.get("worker_id") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "attempt_id": str(payload.get("attempt_id") or ""),
        "contract_hash": str(payload.get("contract_hash") or ""),
        "instance_id": str(payload.get("instance_id") or ""),
        "binding_generation": _nonnegative_int(payload.get("binding_generation")),
        "fencing_generation": _nonnegative_int(payload.get("fencing_generation")),
    }


def build_terminal_output_claim(
    fence: dict[str, Any],
    provided: dict[str, Any],
    *,
    state: str,
    created_at: str,
) -> dict[str, Any]:
    identity = normalize_acceptance_identity(provided)
    return {
        "schema_version": 1,
        "claim_kind": "visible_terminal_output",
        "task_protocol_version": TERMINAL_PROTOCOL_VERSION,
        "terminal_protocol_version": TERMINAL_PROTOCOL_VERSION,
        "terminal_acceptance_required": True,
        **identity,
        "state": str(state or ""),
        "acceptance_fence_hash": acceptance_fence_hash(fence),
        "created_at": created_at,
    }


def recovery_authorization_hash(
    authorization: dict[str, Any],
) -> str:
    return hashlib.sha256(
        json.dumps(
            authorization,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def recovery_activity_snapshot(
    task_dir: Path,
    *,
    include_report: bool = False,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    activity_files = (
        (*RECOVERY_ACTIVITY_FILES, "report.md")
        if include_report
        else RECOVERY_ACTIVITY_FILES
    )
    for name in activity_files:
        path = task_dir / name
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        records.append(
            {
                "path": name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest,
            }
        )
    snapshot_hash = hashlib.sha256(
        json.dumps(
            records,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "activity_files": records,
        "activity_snapshot_hash": snapshot_hash,
    }


def recovery_consumption_hash(
    consumption: dict[str, Any],
) -> str:
    return hashlib.sha256(
        json.dumps(
            consumption,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_operator_recovery_claim(
    fence: dict[str, Any],
    authorization: dict[str, Any],
    consumption: dict[str, Any],
    *,
    state: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "claim_kind": OPERATOR_RECOVERY_CLAIM_KIND,
        "task_protocol_version": TERMINAL_PROTOCOL_VERSION,
        "terminal_protocol_version": TERMINAL_PROTOCOL_VERSION,
        "terminal_acceptance_required": True,
        **normalize_acceptance_identity(fence),
        "state": str(state or ""),
        "acceptance_fence_hash": acceptance_fence_hash(fence),
        "recovery_authorization_hash": recovery_authorization_hash(
            authorization
        ),
        "recovery_consumption_hash": recovery_consumption_hash(
            consumption
        ),
        "created_at": created_at,
    }


def compare_acceptance_identity(
    fence: dict[str, Any],
    claim: dict[str, Any],
) -> list[str]:
    expected = normalize_acceptance_identity(fence)
    actual = normalize_acceptance_identity(claim)
    return [
        field
        for field in ACCEPTANCE_FIELDS
        if expected[field] != actual[field]
    ]


def operator_recovery_acceptance_mismatches(
    task_dir: Path,
    fence: dict[str, Any],
    claim: dict[str, Any],
    authorization: dict[str, Any] | None = None,
    consumption: dict[str, Any] | None = None,
    *,
    require_consumption: bool = True,
) -> list[str]:
    authorization = (
        authorization
        if isinstance(authorization, dict)
        else read_json_object(
            task_dir / OPERATOR_RECOVERY_AUTHORIZATION_FILE
        )
    )
    consumption = (
        consumption
        if isinstance(consumption, dict)
        else read_json_object(
            task_dir / OPERATOR_RECOVERY_CONSUMPTION_FILE
        )
    )
    mismatched: list[str] = []
    if (
        str(claim.get("claim_kind") or "")
        != OPERATOR_RECOVERY_CLAIM_KIND
    ):
        mismatched.append("claim_kind")
    if (
        str(authorization.get("authorization_kind") or "")
        != OPERATOR_RECOVERY_AUTHORIZATION_KIND
    ):
        mismatched.append("recovery_authorization_kind")
    if authorization.get("authorized") is not True:
        mismatched.append("recovery_authorized")
    if not str(authorization.get("authorization_id") or ""):
        mismatched.append("recovery_authorization_id")
    if (
        str(authorization.get("worker_id") or "")
        != str(fence.get("worker_id") or "")
    ):
        mismatched.append("recovery_worker_id")
    if (
        str(authorization.get("task_id") or "")
        != task_dir.name
        or str(authorization.get("task_id") or "")
        != str(fence.get("task_id") or "")
    ):
        mismatched.append("recovery_task_id")
    if (
        str(authorization.get("run_id") or "")
        != task_dir.parent.parent.name
    ):
        mismatched.append("recovery_run_id")
    state = str(claim.get("state") or "")
    if (
        state not in {"blocked", "fail"}
        or str(authorization.get("state") or "") != state
    ):
        mismatched.append("recovery_state")
    if not str(authorization.get("reason") or "").strip():
        mismatched.append("recovery_reason")
    fence_hash = acceptance_fence_hash(fence)
    if (
        str(authorization.get("acceptance_fence_hash") or "")
        != fence_hash
    ):
        mismatched.append("recovery_acceptance_fence_hash")
    if (
        str(claim.get("recovery_authorization_hash") or "")
        != recovery_authorization_hash(authorization)
    ):
        mismatched.append("recovery_authorization_hash")
    report_path = task_dir / "report.md"
    try:
        report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    except OSError:
        report_hash = ""
    if (
        not report_hash
        or str(authorization.get("report_sha256") or "")
        != report_hash
    ):
        mismatched.append("recovery_report_sha256")
    activity = authorization.get("activity_guard")
    if not isinstance(activity, dict):
        mismatched.append("recovery_activity_guard")
    else:
        latest_age = _nonnegative_number(
            activity.get("latest_activity_age_seconds")
        )
        minimum_age = _nonnegative_number(
            activity.get("min_active_age_seconds")
        )
        if (
            latest_age is None
            or minimum_age is None
            or latest_age < minimum_age
        ):
            mismatched.append("recovery_activity_guard")
        authorized_snapshot_hash = str(
            activity.get("activity_snapshot_hash") or ""
        )
        if not authorized_snapshot_hash:
            mismatched.append("recovery_activity_snapshot")
        elif require_consumption:
            if (
                str(consumption.get("activity_snapshot_hash") or "")
                != authorized_snapshot_hash
            ):
                mismatched.append("recovery_activity_snapshot")
        elif (
            recovery_activity_snapshot(task_dir).get(
                "activity_snapshot_hash"
            )
            != authorized_snapshot_hash
        ):
            mismatched.append("recovery_activity_snapshot")
    if require_consumption:
        authorization_hash = recovery_authorization_hash(authorization)
        if (
            str(consumption.get("consumption_kind") or "")
            != OPERATOR_RECOVERY_CLAIM_KIND
        ):
            mismatched.append("recovery_consumption_kind")
        if (
            str(consumption.get("authorization_hash") or "")
            != authorization_hash
        ):
            mismatched.append("recovery_consumption_authorization_hash")
        if (
            str(consumption.get("authorization_id") or "")
            != str(authorization.get("authorization_id") or "")
        ):
            mismatched.append("recovery_consumption_authorization_id")
        if (
            str(consumption.get("worker_id") or "")
            != str(fence.get("worker_id") or "")
        ):
            mismatched.append("recovery_consumption_worker_id")
        if (
            str(consumption.get("task_id") or "")
            != task_dir.name
        ):
            mismatched.append("recovery_consumption_task_id")
        if (
            str(consumption.get("state") or "")
            != state
        ):
            mismatched.append("recovery_consumption_state")
        if not str(consumption.get("consumed_at") or ""):
            mismatched.append("recovery_consumed_at")
        if (
            str(claim.get("recovery_consumption_hash") or "")
            != recovery_consumption_hash(consumption)
        ):
            mismatched.append("recovery_consumption_hash")
    return sorted(set(mismatched))


def recovery_stop_freshness(
    root: Path,
    task_dir: Path,
    fence: dict[str, Any],
    authorization: dict[str, Any],
    *,
    dispatch: Any,
) -> dict[str, Any]:
    errors: list[str] = []
    result: dict[str, Any] = {"ok": False, "errors": errors}
    worker = str(fence.get("worker_id") or "")
    session = str(fence.get("session_id") or "")
    run, task = task_dir.parent.parent.name, task_dir.name
    writer_path = Path(str(authorization.get("writer_evidence_file") or ""))
    if not writer_path.is_absolute() or not writer_path.resolve().is_relative_to(task_dir.resolve()):
        errors.append("current task-contained writer verification is required; unknown writers block")
        return result
    writer = read_json_object(writer_path)
    common_errors, checked_at = dispatch._writer_coverage_common_errors(writer, task_dir, run=run, task=task)
    errors.extend(common_errors)
    if writer.get("schema_version") != 1 or writer.get("record_kind") != "p4_resume_writer_verification":
        errors.append("writer verification kind/schema mismatch")
    if writer.get("fence_hash") != dispatch._canonical_json_hash(fence):
        errors.append("writer verification fence mismatch")
    record = dispatch.worker_record(root, worker)
    if record.get("backend") == "codex_tui":
        return _native_recovery_stop_freshness(
            root, task_dir, fence, authorization, dispatch=dispatch,
            writer=writer, writer_path=writer_path, checked_at=checked_at,
            errors=errors,
        )
    interrupt_id = str(authorization.get("interrupt_id") or "")
    stop_ms = 0.0
    if interrupt_id:
        dispatch.safe_id(interrupt_id, "interrupt-id")
        interrupt = read_json_object(task_dir / f"interrupt-{interrupt_id}.json")
        errors.extend(dispatch._resume_interrupt_errors(
            interrupt, fence, run=run, task=task, worker_id=worker,
            attempt_id=str(fence.get("attempt_id") or ""),
        ))
        if interrupt.get("interrupt_id") != interrupt_id:
            errors.append("interrupt receipt id mismatch")
        errors.extend(dispatch._resume_writer_verification_errors(
            writer, task_dir, fence, interrupt_id=interrupt_id,
            run=run, task=task, interrupt_record=interrupt,
        ))
        reserved = dispatch._parse_iso_utc(str(interrupt.get("reserved_at") or ""))
        delivered = dispatch._parse_iso_utc(str(interrupt.get("delivered_at") or ""))
        if reserved is None or delivered is None or reserved > delivered:
            errors.append("interrupt stop interval is unknown")
        else:
            # The reservation precedes the abort: a turn entering during the
            # transport must not inherit its ambiguous cancellation coverage.
            stop_ms = reserved.timestamp() * 1000
    elif writer.get("interrupt_id"):
        errors.append("old interrupt writer verification cannot become natural-turn stop proof")
    if errors:
        return result
    adapter = dispatch.load_adapter_module(root)
    _record, _binding, host, port = adapter._visible_worker_local_binding(root, worker)
    fetched = dispatch.fetch_opencode_session_message(f"http://{host}:{port}", session, limit=32)
    if not isinstance(fetched, dict) or fetched.get("ok") is False:
        errors.append("bounded exact-session messages unknown/failed")
        return result
    messages = fetched.get("payload")
    if not isinstance(messages, list) or not messages or len(messages) > 32:
        errors.append("bounded exact-session messages insufficient")
        return result
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if (not isinstance(info, dict) or info.get("sessionID") != session
                or info.get("role") not in {"user", "assistant"}):
            errors.append("bounded message session/shape mismatch")
            return result
    start, end = dispatch._task_message_segment(messages, {
        "OMX_RUN_ID": run, "OMX_TASK_ID": task, "OMX_WORKER_ID": worker,
        "OMX_ATTEMPT_ID": str(fence.get("attempt_id") or ""),
    })
    if start < 0 or end != len(messages):
        errors.append("exact task/attempt anchor not observed in bounded tail")
        return result
    users = [m for m in messages[start:] if m["info"].get("role") == "user"]
    if any(not m["info"].get("id") or dispatch._message_created_ms(m) <= 0 for m in users):
        errors.append("human turn identity/time unknown")
        return result
    latest = max(users, key=dispatch._message_created_ms)
    latest_ms = dispatch._message_created_ms(latest)
    result.update(session_id=session, latest_user_id=latest["info"]["id"], latest_user_created_ms=latest_ms)
    if interrupt_id:
        if latest_ms >= stop_ms:
            errors.append("newer human turn invalidates old stopped proof even when idle; obtain current stopped/writer-exit evidence")
    else:
        assistants = [m for m in messages[start:] if m["info"].get("role") == "assistant"]
        final = assistants[-1] if assistants else {}
        info = final.get("info", {})
        completed = dispatch._message_completed_ms(final)
        error = info.get("error")
        aborted = isinstance(error, dict) and error.get("name") == "MessageAbortedError"
        if (info.get("parentID") != latest["info"]["id"] or completed <= latest_ms
                or not (aborted or info.get("finish") in {"stop", "end_turn", "length", "error"})):
            errors.append("current exact turn has no terminal stopped evidence")
        stop_ms = completed
    if checked_at is None or checked_at.timestamp() * 1000 < max(stop_ms, latest_ms):
        errors.append("writer verification predates current stopped turn")
    for message in messages[start:]:
        if (checked_at is not None and max(
                dispatch._message_created_ms(message), dispatch._message_completed_ms(message)
        ) > checked_at.timestamp() * 1000):
            errors.append("message progress after writer verification invalidates coverage")
        parts = message.get("parts")
        if not isinstance(parts, list):
            errors.append("tool writer state unknown")
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "tool":
                state = part.get("state")
                if not isinstance(state, dict) or state.get("status") not in {"completed", "error"}:
                    errors.append("active or unknown tool writer blocks closeout")
    status = adapter._read_session_status_entry(host, port, session)
    if status.get("ok") is not True or status.get("session_id") != session or status.get("status_type") != "idle":
        errors.append("exact-session quiet observation unknown/non-idle; idle alone is never stopped proof")
    if read_json_object(writer_path) != writer:
        errors.append("writer verification changed during observation")
    if interrupt_id and read_json_object(task_dir / f"interrupt-{interrupt_id}.json") != interrupt:
        errors.append("interrupt receipt changed during observation")
    result["ok"] = not errors
    return result


def _native_recovery_stop_freshness(
    root: Path, task_dir: Path, fence: dict[str, Any], authorization: dict[str, Any],
    *, dispatch: Any, writer: dict[str, Any], writer_path: Path,
    checked_at: Any, errors: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "errors": errors}
    worker, session = str(fence.get("worker_id") or ""), str(fence.get("session_id") or "")
    run, task = task_dir.parent.parent.name, task_dir.name
    interrupt_id = str(authorization.get("interrupt_id") or "")
    interrupt: dict[str, Any] = {}
    if interrupt_id:
        dispatch.safe_id(interrupt_id, "interrupt-id")
        interrupt = read_json_object(task_dir / f"interrupt-{interrupt_id}.json")
        expected = {"record_kind": "visible_task_interrupt", "interrupt_id": interrupt_id,
                    "run": run, "task": task, "worker_id": worker,
                    "attempt_id": str(fence.get("attempt_id") or ""),
                    "session_id": session, "fence_hash": dispatch._canonical_json_hash(fence),
                    "status": "sent", "transport": "codex_appserver_websocket",
                    "endpoint": "turn/interrupt"}
        if any(interrupt.get(key) != value for key, value in expected.items()) or interrupt.get("backend_accepted") is not True:
            errors.append("native interrupt identity/accepted transport mismatch")
        response = interrupt.get("response")
        if (not isinstance(response, dict) or response.get("thread") != session
                or response.get("phase") != "interrupt" or response.get("outcome") != "interrupt_sent"
                or not isinstance(response.get("response"), dict)):
            errors.append("native interrupt has no accepted exact-thread RPC response")
        errors.extend(dispatch._resume_writer_verification_errors(
            writer, task_dir, fence, interrupt_id=interrupt_id, run=run, task=task,
            interrupt_record=interrupt,
        ))
        reserved = dispatch._parse_iso_utc(str(interrupt.get("reserved_at") or ""))
        delivered = dispatch._parse_iso_utc(str(interrupt.get("delivered_at") or ""))
        if reserved is None or delivered is None or reserved > delivered:
            errors.append("native interrupt stop interval unknown")
    elif writer.get("interrupt_id"):
        errors.append("old native interrupt writer verification cannot become natural stop proof")
    if errors:
        return result
    receipt_path = task_dir / "codex_turn" / "turn-receipt.json"
    receipt = read_json_object(receipt_path)
    observed = dispatch.load_codex_adapter_module(root).observe_recovery_stop(
        root, worker, run=run, task=task, session_id=session,
    )
    turns = observed.get("turns")
    if (observed.get("ok") is not True or observed.get("session_id") != session
            or not isinstance(turns, list) or not turns):
        errors.append("bounded exact native thread/task turns unknown")
        return result
    task_turn = observed.get("task_turn_id")
    if interrupt_id:
        if interrupt["response"].get("turn") != task_turn or turns[0].get("id") != task_turn:
            errors.append("newer native turn invalidates exact-task interrupt even when idle")
        if turns[0].get("status") not in {"completed", "interrupted", "failed"}:
            errors.append("native task turn is not terminal after interrupt")
    for turn in turns:
        completed = turn.get("completedAt")
        if (turn.get("status") not in {"completed", "interrupted", "failed"}
                or type(completed) is not int or completed <= 0
                or checked_at is None or completed >= checked_at.timestamp()):
            errors.append("native terminal timing unknown or writer verification not after turn completion")
    if read_json_object(writer_path) != writer or read_json_object(receipt_path) != receipt:
        errors.append("native writer/task receipt changed during observation")
    if interrupt_id and read_json_object(task_dir / f"interrupt-{interrupt_id}.json") != interrupt:
        errors.append("native interrupt receipt changed during observation")
    result.update(ok=not errors, session_id=session, latest_turn_id=turns[0].get("id"),
                  task_turn_id=task_turn, source=observed.get("source"))
    return result


def terminal_acceptance_status(task_dir: Path) -> dict[str, Any]:
    fence_path = task_dir / "acceptance_fence.json"
    claim_path = task_dir / "terminal_output_claim.json"
    classification = classify_terminal_protocol(task_dir)
    fence = read_json_object(fence_path)
    claim = read_json_object(claim_path)
    protocol_classification = str(
        classification.get("classification") or ""
    )
    if (
        protocol_classification == TERMINAL_PROTOCOL_CLASSIFICATION_LEGACY
        and not fence
    ):
        return {
            "required": False,
            "accepted": True,
            "reason": "legacy_task_without_acceptance_fence",
            "mismatched_fields": [],
            "fence": {},
            "claim": claim,
            "protocol_classification": protocol_classification,
        }
    if protocol_classification == TERMINAL_PROTOCOL_CLASSIFICATION_INVALID:
        invalid_fields = list(classification.get("mismatched_fields") or [])
        pending_empty_recovery_closeout = bool(
            fence
            and str(claim.get("claim_kind") or "")
            == OPERATOR_RECOVERY_CLAIM_KIND
            and invalid_fields == ["session_id"]
        )
        if not pending_empty_recovery_closeout:
            if not fence:
                invalid_fields.append("acceptance_fence")
            return {
                "required": True,
                "accepted": False,
                "reason": "invalid_terminal_protocol_identity",
                "mismatched_fields": sorted(set(invalid_fields)),
                "fence": fence,
                "claim": claim,
                "protocol_classification": protocol_classification,
            }
        protocol_classification = TERMINAL_PROTOCOL_CLASSIFICATION_MODERN
    if not fence:
        return {
            "required": True,
            "accepted": False,
            "reason": "terminal_acceptance_fence_missing",
            "mismatched_fields": ["acceptance_fence"],
            "fence": {},
            "claim": claim,
            "protocol_classification": protocol_classification,
        }
    if not claim:
        return {
            "required": True,
            "accepted": False,
            "reason": "terminal_output_claim_missing",
            "mismatched_fields": list(ACCEPTANCE_FIELDS),
            "fence": fence,
            "claim": {},
            "protocol_classification": protocol_classification,
        }
    mismatched = compare_acceptance_identity(fence, claim)
    mismatched.extend(_invalid_generation_fields(fence))
    mismatched.extend(_invalid_generation_fields(claim))
    hash_matches = str(claim.get("acceptance_fence_hash") or "") == acceptance_fence_hash(fence)
    if not hash_matches:
        mismatched.append("acceptance_fence_hash")
    if (
        str(claim.get("claim_kind") or "")
        == OPERATOR_RECOVERY_CLAIM_KIND
    ):
        authorization = read_json_object(
            task_dir / OPERATOR_RECOVERY_AUTHORIZATION_FILE
        )
        mismatched.extend(
            operator_recovery_acceptance_mismatches(
                task_dir,
                fence,
                claim,
                authorization,
            )
        )
        terminal_markers = [
            marker
            for marker in (".done", ".fail", ".blocked")
            if (task_dir / marker).is_file()
        ]
        if terminal_markers != [f".{claim.get('state') or ''}"]:
            mismatched.append("recovery_terminal_marker")
        accepted = not mismatched
        return {
            "required": True,
            "accepted": accepted,
            "reason": (
                "accepted_operator_recovery_closeout"
                if accepted
                else "operator_recovery_claim_mismatch"
            ),
            "mismatched_fields": sorted(set(mismatched)),
            "fence": fence,
            "claim": claim,
            "recovery_authorization": authorization,
            "protocol_classification": protocol_classification,
        }
    live_observation = (
        claim.get("live_session_observation")
        if isinstance(claim.get("live_session_observation"), dict)
        else {}
    )
    expected_session_id = str(fence.get("session_id") or "")
    session_fence_state = str(fence.get("session_fence_state") or "")
    if (
        "session_fence_state" in fence
        and session_fence_state != "live_observed"
    ):
        mismatched.append("session_fence_state")
    # Security contract: OpenCode keeps the strict ses_ check; a codex_tui
    # task accepts its native thread id only when it equals the thread in
    # this task's own delivery record (never an arbitrary UUID).
    task_backend = ""
    trusted_codex_thread = ""
    try:
        task_record = read_json_object(task_dir / "task.json")
        task_backend = str(task_record.get("backend") or "")
        delivery = read_json_object(task_dir / "delivery.json")
        trusted_codex_thread = str(delivery.get("codex_thread") or "")
    except (OSError, ValueError):
        pass
    session_id_valid = expected_session_id.startswith("ses_")
    if not session_id_valid and task_backend == "codex_tui":
        session_id_valid = bool(
            trusted_codex_thread
            and expected_session_id == trusted_codex_thread
        )
    if (
        not session_id_valid
        or not live_observation
        or not bool(live_observation.get("matches"))
        or str(live_observation.get("expected_session_id") or "")
        != expected_session_id
        or expected_session_id
        not in {
            str(item or "")
            for item in live_observation.get("session_ids", [])
        }
    ):
        mismatched.append("live_session_id")
    return {
        "required": True,
        "accepted": not mismatched,
        "reason": "accepted" if not mismatched else "terminal_output_claim_mismatch",
        "mismatched_fields": sorted(set(mismatched)),
        "fence": fence,
        "claim": claim,
        "protocol_classification": protocol_classification,
    }


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _invalid_generation_fields(payload: dict[str, Any]) -> list[str]:
    invalid: list[str] = []
    for field in ("binding_generation", "fencing_generation"):
        try:
            parsed = int(payload.get(field))
        except (TypeError, ValueError):
            invalid.append(field)
            continue
        if parsed < 0:
            invalid.append(field)
    return invalid


def _nonnegative_int_or_none(payload: dict[str, Any], field: str) -> int | None:
    try:
        parsed = int(payload.get(field))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def current_host_boot_id() -> str:
    """Read the host boot id; empty when the host does not expose it."""
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError:
        return ""


def quarantined_owner_stopped_evidence(
    root: Path,
    task_dir: Path,
    fence: dict[str, Any],
    *,
    worker_id: str,
) -> dict[str, Any]:
    """Validate quarantine provenance for an old-attempt recovery closeout.

    After a reviewed reboot-stale quarantine archived the old owner lock and
    the worker was rebound, the current binding/owner lock legitimately
    belong to newer work and must not be compared against the old fence.
    This evidence replaces that comparison: the archived lock must carry the
    old fence identity, and the old writer's host boot generation must still
    be gone (current boot id differs). It never treats the new owner lock as
    the old actor and never fabricates scientific completion.
    """
    quarantine = read_json_object(task_dir / "quarantine.json")
    run_id = task_dir.parent.parent.name
    task_id = task_dir.name
    fence_binding = _nonnegative_int_or_none(fence, "binding_generation")
    fence_fencing = _nonnegative_int_or_none(fence, "fencing_generation")
    archived_rel = str(quarantine.get("archived_lock") or "")
    archived: dict[str, Any] = {}
    archived_path: Path | None = None
    if archived_rel:
        candidate = root / archived_rel
        try:
            candidate.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            candidate = None
        if candidate is not None and candidate.is_file():
            archived_path = candidate
            archived = read_json_object(candidate)
    archived_binding = _nonnegative_int_or_none(
        archived, "binding_generation"
    )
    archived_fencing = _nonnegative_int_or_none(
        archived, "fencing_generation"
    )
    archived_boot_id = str(archived.get("host_boot_id") or "")
    boot_id = current_host_boot_id()
    fence_worker = str(fence.get("worker_id") or "")
    identity_matches = bool(
        archived
        and str(archived.get("run") or "") == run_id
        and str(archived.get("task") or "") == task_id
        and str(archived.get("worker_id") or "") == fence_worker
        and str(archived.get("owner_lock_id") or "")
        and archived_binding is not None
        and archived_binding == fence_binding
        and (
            archived_fencing is None
            or fence_fencing is None
            or archived_fencing == fence_fencing
        )
    )
    conditions = {
        "quarantine_record_valid": bool(
            quarantine
            and quarantine.get("schema_version") == 1
            and str(quarantine.get("quarantine_state") or "")
            == "quarantined"
            and str(quarantine.get("run_id") or "") == run_id
            and str(quarantine.get("task_id") or "") == task_id
            and bool(fence_worker)
            and str(quarantine.get("worker_id") or "") == fence_worker
            and fence_worker == worker_id
        ),
        "quarantine_basis_is_reboot_stale": (
            str(quarantine.get("quarantine_basis") or "")
            == QUARANTINE_REBOOT_CLOSEOUT_BASIS
        ),
        "archived_lock_present": archived_path is not None and bool(archived),
        "archived_lock_identity_matches_fence": identity_matches,
        "old_writer_boot_generation_stopped": bool(
            archived_boot_id
            and boot_id
            and archived_boot_id != boot_id
        ),
    }
    return {
        "schema_version": 1,
        "evidence_kind": "quarantined_owner_stopped",
        "ok": all(conditions.values()),
        "conditions": conditions,
        "quarantine_basis": str(quarantine.get("quarantine_basis") or ""),
        "archived_lock": archived_rel,
        "archived_owner_lock_id": str(archived.get("owner_lock_id") or ""),
        "archived_binding_generation": archived_binding,
        "archived_host_boot_id": archived_boot_id,
        "current_host_boot_id": boot_id,
        "note": (
            "reboot-stale quarantine provenance: archived old-lock identity "
            "plus a still-different host boot generation prove the old "
            "writer stopped; the current binding/owner lock are newer work "
            "and are never treated as the old actor"
        ),
    }
