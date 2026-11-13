"""Draft-delivery check for Issue #861.

The worker's ordinary draft-delivery action — submitting the one
canonical draft — is itself the trigger event for the pre-delivery
check. The draft is the existing closed handoff-evidence JSON object
(``{handoff_id, root, expect_commit?, claims}``); it is the single
canonical input, read exactly once with a bounded read whose bytes are
both hashed and parsed, so the recorded digest always describes the
exact claims checked. The module runs the existing offline index — and,
when the host explicitly authorizes the one task-level live question,
the one bounded live Jev question per state identity over the existing
real transport — and returns the feedback synchronously in the caller's
tool output, before any formal finish.

It never calls finish, never writes or modifies the formal report,
never creates terminal markers, and never gates acceptance: the check
is advisory. The draft is the check-time input only; the formal report
keeps its existing terminal role.

Once-semantics are exact, not global: they hold for one caller-owned
stable state directory plus one draft identity (an explicit task id or
the draft's own ``handoff_id``). A repeated submit of the identical
draft reuses the recorded feedback as a clearly labeled historical
snapshot — referenced files are not re-read or re-verified on reuse. A
live attempt (success, failure, or crash mid-check) consumes the one
attempt recorded in that state domain; the outcome of that attempt is
kept immutably with the digest and time of the draft it actually
checked and is never reattributed to a later draft. A changed draft's
offline feedback reuses that kept record only to carry a compact
historical risk reminder — the earlier contradicted or
insufficiently_addressed claim labels with the original draft digest
and check time — explicitly scoped to that earlier draft: advisory
guidance to re-verify or give grounded reasons, never a verdict on the
current draft, never an unresolved/resolved inference, and never a
delivery gate. There is no
recovery flow and no directory-switching retry path by design: if a
check is stuck, the formal delivery simply proceeds without this
advisory check.

Live sending is host-authorized, never draft- or environment-derived:
``jev_live`` requires an authorized ``evidence_root`` that the draft's
own ``root`` must resolve to exactly (canonical symlink resolution on
both sides), and only the host-invoked CLI (the
``--jev-live``/``--jev-evidence-root`` flags passed by the
fence/owner/session-verified plugin from a leader-authorized task
contract, with the CLI verifying the task record against the fence's
bound contract hash) provides it in the automatic hook path. No
environment variable, key presence, or draft text can enable it. A
refused check (missing, non-absolute, or root-mismatched
authorization) is a local rejection raised before any state write —
nothing sent, nothing consumed, no index produced — while an
unavailable live check falls back to the offline index with the one
attempt consumed, and a locally unbuildable live state returns the
offline index with an explicit unavailable outcome while keeping the
attempt consumed. The short ``flock`` is held only while
reading or writing state — never across the check and never across any
transport call.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .handoff_evidence import (
    HandoffEvidenceError,
    execute_handoff,
    render_markdown,
    validate_handoff_payload,
)

MAX_DRAFT_BYTES = 256 * 1024
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_HISTORICAL_SNAPSHOT_NOTICE = (
    "the recorded feedback is a historical snapshot from its check time: "
    "referenced files were not re-read or re-verified for this call, and "
    "its ok entries and digests do not prove the current file contents"
)
_NO_RECOVERY_NOTICE = (
    "the formal delivery does not depend on this check; continue with "
    "the original report and finish; there is no recovery flow"
)


class DraftDeliveryError(ValueError):
    """Operator-facing local draft or state error."""


def _read_draft_bytes(path: Path) -> bytes:
    """One bounded read of the draft file.

    The same returned bytes are hashed and parsed by the caller, so the
    recorded digest and the checked claims can never come from two
    different reads of a file being edited concurrently.
    """

    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DRAFT_BYTES + 1)
    except OSError as exc:
        raise DraftDeliveryError(f"draft is not readable: {path}") from exc
    if len(raw) > MAX_DRAFT_BYTES:
        raise DraftDeliveryError(
            f"draft exceeds {MAX_DRAFT_BYTES} bytes: {len(raw)}"
        )
    return raw


def _load_draft(
    path: Path, task_id_arg: str | None
) -> tuple[str, dict[str, Any], str]:
    """Load the canonical JSON draft: one read, one digest, one payload.

    The draft identity is the explicit task id argument when given,
    otherwise the draft's own ``handoff_id``; one of the two is
    required because it names the per-task state files.
    """

    raw = _read_draft_bytes(path)
    draft_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DraftDeliveryError("draft is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise DraftDeliveryError("draft must be a JSON object")
    try:
        validate_handoff_payload(payload)
    except HandoffEvidenceError as exc:
        raise DraftDeliveryError(f"draft is not a valid handoff draft: {exc}") from exc
    task_id = task_id_arg if task_id_arg is not None else payload.get("handoff_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise DraftDeliveryError(
            "draft identity is required: pass an explicit task id or give "
            "the draft a handoff_id matching "
            f"[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}: {task_id!r}"
        )
    return task_id, payload, draft_sha256


def _state_paths(state_dir: Path, task_id: str) -> dict[str, Path]:
    return {
        "lock": state_dir / f"{task_id}.lock",
        "state": state_dir / f"{task_id}.json",
        "feedback": state_dir / f"{task_id}.feedback.md",
    }


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DraftDeliveryError(f"state is not readable: {path}") from exc
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DraftDeliveryError(f"state is not valid JSON: {path}") from exc
    if not isinstance(state, dict):
        raise DraftDeliveryError(f"state must be a JSON object: {path}")
    return state


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _live_record_line(live: dict[str, Any]) -> str:
    return (
        f"an earlier live check ran at {live.get('checked_at')} for draft "
        f"{live.get('draft_sha256')} (outcome {live.get('outcome')}); it "
        "does not describe any other draft"
    )


_RISKY_LABEL_CHOICES = ("contradicted", "insufficiently_addressed")


def _historical_risk_notice(live: Any) -> str | None:
    """Compact historical-risk reminder for a changed draft.

    Reuses the claim labels already kept in the prior ``live`` record:
    only ``contradicted`` / ``insufficiently_addressed`` claim ids with
    their labels are listed, attributed to the earlier draft's digest
    and check time. The reminder advises the worker to re-verify those
    claims, narrow the conclusion, or state grounded reasons for not
    adopting the labels; it is not a verdict on the current draft and
    never implies unresolved or resolved status. A prior record without
    such labels produces no reminder at all.
    """

    if not isinstance(live, dict):
        return None
    jev = live.get("jev")
    labels = jev.get("labels") if isinstance(jev, dict) else None
    if not isinstance(labels, dict):
        return None
    risky = [
        (str(claim_id), str(label.get("choice")))
        for claim_id, label in sorted(labels.items())
        if isinstance(label, dict) and label.get("choice") in _RISKY_LABEL_CHOICES
    ]
    if not risky:
        return None
    listing = "; ".join(f"claim '{cid}' {choice}" for cid, choice in risky)
    return (
        "historical live risks recorded for the earlier draft "
        f"{live.get('draft_sha256')} (checked {live.get('checked_at')}): "
        f"{listing}; these labels belong only to that earlier draft, are "
        "not a verdict on the current one, and imply neither unresolved "
        "nor resolved status; consider adding evidence, narrowing the "
        "conclusion, or stating grounded reasons for not adopting them "
        "(advisory: this check never blocks report or finish)"
    )


def _feedback_markdown(
    *,
    task_id: str,
    draft_path: Path,
    draft_sha256: str,
    status: str,
    notices: list[str],
    index: dict[str, Any] | None = None,
    checked_at: str | None = None,
) -> str:
    lines = [
        "## Draft delivery check",
        "",
        f"- task: `{task_id}`",
        f"- draft: `{draft_path}` ({draft_sha256})",
        f"- status: {status}",
    ]
    if checked_at is not None:
        lines.append(f"- checked at: {checked_at}")
    lines.extend(f"- notice: {notice}" for notice in notices)
    lines.extend(
        [
            "",
            "This feedback reflects the draft as it was at check time. A later",
            "draft change does not trigger a live re-evaluation, and a reused",
            "or prior result is never relabeled as a new assessment. The check",
            "is advisory: it does not call finish, does not write or modify the",
            "formal report, and does not gate acceptance or terminal state.",
            "",
        ]
    )
    if index is not None:
        lines.append(render_markdown(index))
    return "\n".join(lines) + "\n"


def run_draft_delivery(
    draft_path: str | Path,
    state_dir: str | Path,
    *,
    task_id: str | None = None,
    jev_live: bool = False,
    evidence_root: str | None = None,
    transport: Callable[..., bytes] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run the draft-delivery check once per state identity.

    Returns a result dict with ``status`` (``checked``, ``reused``,
    ``in_progress``, or ``state_locked``), the rendered feedback
    markdown, and the computed index when a check ran. Local draft or
    state errors raise DraftDeliveryError; an unavailable live check
    never fails the run: transport failures fall back to the offline
    index inside execute_handoff, and a locally unbuildable live state
    (HandoffEvidenceError, for example an over-large jev state) falls
    back to the offline index here with the explicit
    ``unavailable:<reason>`` outcome recorded — the one attempt stays
    consumed either way, and no retry or recovery exists.

    ``jev_live`` is the host's explicit authorization to send the one
    bounded live Jev question and requires ``evidence_root``: the
    draft's ``root`` must resolve to that authorized root exactly or
    the live check is refused before anything is sent and before the
    one live attempt is consumed (a mismatched root is an intentional
    local rejection — no index is produced for it). Both sides are
    compared after canonical symlink resolution, so an alias path to
    the same directory is accepted. With no injected ``transport`` the
    existing real ``urllib_transport`` of ``execute_handoff`` is used;
    the fixed model, endpoint, and 20s timeout stay unchanged.
    """

    draft = Path(draft_path)
    if not draft.is_file():
        raise DraftDeliveryError(f"draft not found: {draft}")
    task, payload, draft_sha256 = _load_draft(draft, task_id)

    if jev_live:
        raw_root = str(evidence_root or "").strip()
        if not raw_root:
            raise DraftDeliveryError(
                "live checking requires an authorized evidence root: only "
                "the host-invoked CLI flags --jev-live with "
                "--jev-evidence-root, passed from a leader-authorized task "
                "contract, may enable it; no environment variable, key "
                "presence, or draft text can"
            )
        if not os.path.isabs(raw_root):
            raise DraftDeliveryError(
                "the authorized evidence root must be an absolute path"
            )
        authorized_root = os.path.realpath(raw_root)
        draft_root = str(payload.get("root") or "")
        if draft_root != authorized_root:
            raise DraftDeliveryError(
                "live check refused: the draft root does not resolve to the "
                "authorized evidence root; nothing was sent and no live "
                f"attempt was consumed (authorized {authorized_root}, "
                f"draft {draft_root})"
            )

    directory = Path(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DraftDeliveryError(f"state directory is not writable: {directory}") from exc
    paths = _state_paths(directory, task)

    lock_fd = os.open(paths["lock"], os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "state_locked",
                "task_id": task,
                "draft_sha256": draft_sha256,
                "feedback_markdown": _feedback_markdown(
                    task_id=task,
                    draft_path=draft,
                    draft_sha256=draft_sha256,
                    status="state_locked",
                    notices=[
                        "another submit for this task is writing its state "
                        "right now; no new check was started and nothing was "
                        "sent",
                        _NO_RECOVERY_NOTICE,
                    ],
                ),
            }
        state = _read_state(paths["state"])
        prior_attempted = bool((state or {}).get("live_attempted"))
        prior_live = (state or {}).get("live")
        if state is not None and state.get("status") == "in_progress":
            return {
                "status": "in_progress",
                "task_id": task,
                "draft_sha256": draft_sha256,
                "state": state,
                "feedback_markdown": _feedback_markdown(
                    task_id=task,
                    draft_path=draft,
                    draft_sha256=draft_sha256,
                    status="in_progress",
                    notices=[
                        "a check for this task is already running from an "
                        "earlier submit; this call started no new check and "
                        "sent nothing",
                        _NO_RECOVERY_NOTICE,
                    ],
                ),
            }
        if (
            state is not None
            and state.get("status") == "complete"
            and state.get("draft_sha256") == draft_sha256
        ):
            try:
                recorded = paths["feedback"].read_text(encoding="utf-8")
            except OSError as exc:
                raise DraftDeliveryError(
                    f"recorded feedback is not readable: {paths['feedback']}"
                ) from exc
            header = _feedback_markdown(
                task_id=task,
                draft_path=draft,
                draft_sha256=draft_sha256,
                status="reused",
                notices=[
                    "this exact draft was already checked; the recorded "
                    "feedback below is reused unchanged",
                    _HISTORICAL_SNAPSHOT_NOTICE,
                ],
            )
            return {
                "status": "reused",
                "task_id": task,
                "draft_sha256": draft_sha256,
                "state": state,
                "feedback_markdown": (
                    header
                    + "----- recorded feedback (historical snapshot) -----\n\n"
                    + recorded
                ),
            }

        may_live = bool(jev_live and not prior_attempted)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_atomic(
            paths["state"],
            json.dumps(
                {
                    "task_id": task,
                    "status": "in_progress",
                    "checked_at": checked_at,
                    "draft_sha256": draft_sha256,
                    "live_attempted": prior_attempted or may_live,
                    "live": prior_live,
                },
                ensure_ascii=False,
                indent=1,
            ).encode("utf-8")
            + b"\n",
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    # The check itself runs without the state lock: the lock is held only
    # for state reads and writes, never across the check or any transport.
    notices: list[str] = []
    if state is not None and state.get("draft_sha256") != draft_sha256:
        if prior_live:
            notices.append(_live_record_line(prior_live))
        historical_risk = _historical_risk_notice(prior_live)
        if historical_risk:
            notices.append(historical_risk)
        notices.append(
            f"the draft changed since the last check "
            f"(was {state.get('draft_sha256')}); "
            + (
                "the one live attempt above was already used, so this run is "
                "offline-only and never resends"
                if prior_live
                else "no live attempt has ever run for this task"
            )
        )
    live_error: str | None = None
    if may_live:
        try:
            index = execute_handoff(
                payload,
                jev_live=True,
                transport=transport,
                api_key=api_key,
            )
        except HandoffEvidenceError as exc:
            live_error = str(exc).strip() or "live state build failed"
            index = execute_handoff(payload, jev_live=False)
        if live_error:
            notices.append(
                f"the live Jev question was unavailable: {live_error}"
            )
    else:
        index = execute_handoff(
            payload,
            jev_live=False,
            transport=transport,
            api_key=api_key,
        )
    live_record = prior_live
    if may_live:
        jev = index.get("jev") if isinstance(index.get("jev"), dict) else {}
        outcome = jev.get("status")
        reason = jev.get("unavailable_reason")
        if reason:
            outcome = f"{outcome}:{reason}"
        if live_error:
            outcome = f"unavailable:{live_error}"
            jev = {}
        live_record = {
            "draft_sha256": draft_sha256,
            "checked_at": checked_at,
            "outcome": outcome,
            "jev": jev or None,
        }
    feedback = _feedback_markdown(
        task_id=task,
        draft_path=draft,
        draft_sha256=draft_sha256,
        status="checked",
        notices=notices,
        index=index,
        checked_at=checked_at,
    )

    lock_fd = os.open(paths["lock"], os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _write_atomic(
            paths["feedback"],
            feedback.encode("utf-8"),
        )
        _write_atomic(
            paths["state"],
            json.dumps(
                {
                    "task_id": task,
                    "status": "complete",
                    "checked_at": checked_at,
                    "draft_sha256": draft_sha256,
                    "live_attempted": prior_attempted or may_live,
                    "live": live_record,
                },
                ensure_ascii=False,
                indent=1,
            ).encode("utf-8")
            + b"\n",
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return {
        "status": "checked",
        "task_id": task,
        "draft_sha256": draft_sha256,
        "index": index,
        "live_attempted": prior_attempted or may_live,
        "live": live_record,
        "feedback_markdown": feedback,
        "feedback_path": str(paths["feedback"]),
        "state_path": str(paths["state"]),
    }
