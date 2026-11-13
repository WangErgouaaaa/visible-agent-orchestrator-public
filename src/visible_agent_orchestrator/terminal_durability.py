"""Durable persistence for authoritative terminal closeout artifacts.

Issue #845 observed a host reboot that left a released owner lock and a
success-looking ``protocol_status.json`` while ``terminal_output_claim.json``
was zero bytes and ``events.jsonl`` had a NUL tail. The closeout path wrote
its artifacts without fsync, so an interrupted writeback could persist the
terminal marker and status while losing authoritative claim data.

Durability policy (issue #845 rework):

* Ordinary flow/advisory writes (markers like ``.received``/``.heartbeat``,
  ``protocol_status.json`` refreshes, events, checkpoints) keep plain
  atomic tmp+replace semantics with no fsync.
* Only consequential terminal boundaries are durable:
  :func:`durable_atomic_write` (tmp, fsync, replace, parent-dir fsync) is
  used by ``omx_task_signal`` finish for claim/marker/status publication,
  and by owner-lock release paths for the archived lock record.
* :func:`pin_terminal_prerequisites` flushes the narrow actual-dependency
  set of a terminal closeout: report, modern identity records (task,
  dispatch), acceptance fence, terminal output claim, the run-local
  terminal-protocol creation record, the sole terminal marker, and the
  directory entries that anchor them (task dir, creation dir, tasks dir,
  run dir). Advisory ``protocol_status.json``/``finalizer_event.json``/
  ``events.jsonl`` are deliberately not gates.
* :func:`pin_terminal_authority` is the release gate: prerequisites flushed,
  then the complete terminal contract revalidated from disk (non-empty
  report, exactly one terminal marker, accepted terminal classification).
  An owner lock is only archived when this returns ``ok``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.task_protocol_status import (
    TERMINAL_MARKERS,
    file_size,
)
from visible_agent_orchestrator.terminal_acceptance import terminal_acceptance_status
from visible_agent_orchestrator.terminal_protocol import creation_record_path

TERMINAL_PREREQUISITE_FILES = (
    "report.md",
    "task.json",
    "dispatch.json",
    "acceptance_fence.json",
    "terminal_output_claim.json",
)

PIN_REASON_DURABLE = "durable"
PIN_REASON_FSYNC_FAILED = "terminal_authority_fsync_failed"
PIN_REASON_REPORT_MISSING = "terminal_report_missing_or_empty"
PIN_REASON_MARKER_MISSING = "terminal_marker_missing"
PIN_REASON_MARKER_CONFLICT = "terminal_marker_conflict"

TERMINAL_RECOVERY_ACTION = (
    "terminal authority record is incomplete: preserve all existing "
    "evidence unchanged; inspect read-only with omx_visible_task_state "
    "and omx_visible_dispatch collect; there is no supported automatic "
    "repair (do not backfill terminal_output_claim.json from "
    "protocol_status.json, do not rewrite markers, do not relabel the "
    "old task as accepted, do not blindly rerun or resubmit the business "
    "job); open a new task that independently re-verifies the original "
    "artifacts and their provenance before any reuse"
)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_file_if_present(path: Path) -> bool:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def durable_atomic_write(path: Path, content: str) -> None:
    """Durably replace ``path`` with ``content``.

    The temporary file is fsynced before the replace so the replaced target
    never exposes a zero-length or torn body after a crash, and the parent
    directory is fsynced afterwards so the rename itself survives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def terminal_prerequisite_paths(task_dir: Path) -> list[Path]:
    """Actual closeout dependency files, present ones only."""
    paths = [task_dir / name for name in TERMINAL_PREREQUISITE_FILES]
    paths.append(creation_record_path(task_dir))
    for marker in TERMINAL_MARKERS:
        paths.append(task_dir / f".{marker}")
    return [path for path in paths if path.is_file()]


def pin_terminal_prerequisites(task_dir: Path) -> dict[str, Any]:
    """Flush the terminal closeout dependency set to storage.

    Covers report, modern identity records, fence, claim, creation record,
    present terminal markers, and the directory entries that anchor them
    (task dir, creation dir, tasks dir, run dir). Never raises; a failed
    flush returns ``ok=False`` with the honest error.
    """
    pinned: list[str] = []
    try:
        for path in terminal_prerequisite_paths(task_dir):
            if fsync_file_if_present(path):
                pinned.append(path.name)
        creation_dir = creation_record_path(task_dir).parent
        for directory in (
            task_dir,
            creation_dir,
            task_dir.parent,
            task_dir.parent.parent,
        ):
            if directory.is_dir():
                fsync_directory(directory)
                pinned.append(f"{directory.name}/")
    except OSError as exc:
        return {
            "ok": False,
            "reason": PIN_REASON_FSYNC_FAILED,
            "error": str(exc),
            "pinned": pinned,
        }
    return {"ok": True, "reason": PIN_REASON_DURABLE, "pinned": pinned}


def pin_terminal_authority(task_dir: Path) -> dict[str, Any]:
    """Flush terminal prerequisites and revalidate the complete contract.

    Never raises: owner-lock release paths gate on the returned ``ok``.
    ``ok`` is true only when the prerequisite files were flushed to storage
    AND the freshly recomputed terminal contract holds: non-empty
    ``report.md``, exactly one terminal marker, and an accepted terminal
    classification/identity.
    """
    barrier = pin_terminal_prerequisites(task_dir)
    if not barrier.get("ok"):
        return {
            "ok": False,
            "reason": str(barrier.get("reason") or ""),
            "pinned": barrier.get("pinned", []),
            "acceptance": {},
        }
    report = task_dir / "report.md"
    if file_size(report) <= 0:
        return {
            "ok": False,
            "reason": PIN_REASON_REPORT_MISSING,
            "pinned": barrier.get("pinned", []),
            "acceptance": {},
        }
    markers = [
        marker
        for marker in TERMINAL_MARKERS
        if (task_dir / f".{marker}").is_file()
    ]
    if not markers:
        return {
            "ok": False,
            "reason": PIN_REASON_MARKER_MISSING,
            "pinned": barrier.get("pinned", []),
            "acceptance": {},
        }
    if len(markers) > 1:
        return {
            "ok": False,
            "reason": PIN_REASON_MARKER_CONFLICT,
            "pinned": barrier.get("pinned", []),
            "acceptance": {},
        }
    acceptance = terminal_acceptance_status(task_dir)
    if not acceptance.get("accepted"):
        return {
            "ok": False,
            "reason": str(
                acceptance.get("reason") or "terminal_acceptance_not_accepted"
            ),
            "pinned": barrier.get("pinned", []),
            "acceptance": acceptance,
        }
    return {
        "ok": True,
        "reason": PIN_REASON_DURABLE,
        "pinned": barrier.get("pinned", []),
        "acceptance": acceptance,
    }
