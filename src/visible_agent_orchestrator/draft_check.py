"""Explicitly selected draft-check task option for Issue #861.

A task opts into the draft-delivery check by carrying the closed
``draft_check`` object in its task contract. The option is deliberately
closed: ``{"enabled": true}`` are the only accepted contents for the
default offline check, the draft deliverable is always the fixed
``<task_dir>/draft.json`` path in the existing handoff-evidence JSON
format, and the check state directory is always the fixed
``<task_dir>/draft-check`` directory. Neither the worker nor the draft
text can move, rename, or extend those locations, and tasks without the
object keep their exact existing prompt, report, and finish behavior.

The one live Jev question is likewise leader-selected in the same
closed object: ``{"enabled": true, "jev_live": true, "evidence_root":
<absolute path>}``. Only the leader's public option can produce that
object before the grant/contract hash is prepared; a task document or
draft text can never add it. The recorded ``evidence_root`` is the
authorized evidence root: the check refuses to send unless the draft
declares exactly that root, so a worker draft cannot redirect the
check at a different host directory.

The check itself stays advisory: nothing here changes the report
contract, the finish protocol, or terminal state.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


EXPECTED_KEYS = frozenset({"enabled", "jev_live", "evidence_root"})


class DraftCheckError(ValueError):
    """Raised at dispatch time for a malformed draft_check contract."""


def validate_draft_check_contract(
    task_contract: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the normalized draft-check selection, or None when absent.

    A missing ``draft_check`` field means the task did not opt in. A
    present-but-malformed field is a dispatch-time error so a typo can
    never silently widen or narrow the delivery contract. The live
    selection requires an absolute ``evidence_root`` and normalizes it;
    an ``evidence_root`` without ``jev_live: true`` is rejected so the
    closed object cannot grow fields outside the two documented modes.
    """

    contract = task_contract if isinstance(task_contract, Mapping) else {}
    raw = contract.get("draft_check")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise DraftCheckError("draft_check must be an object")
    unknown = sorted(set(raw) - EXPECTED_KEYS)
    if unknown:
        raise DraftCheckError(
            "draft_check has unknown field(s): " + ", ".join(unknown)
        )
    if raw.get("enabled") is not True:
        raise DraftCheckError("draft_check.enabled must be boolean true")
    jev_live = raw.get("jev_live")
    if jev_live is not None and not isinstance(jev_live, bool):
        raise DraftCheckError("draft_check.jev_live must be boolean")
    evidence_root = raw.get("evidence_root")
    if jev_live is True:
        if not isinstance(evidence_root, str) or not evidence_root.strip():
            raise DraftCheckError(
                "draft_check.evidence_root must be a non-empty absolute "
                "path when jev_live is true"
            )
        normalized = os.path.normpath(evidence_root)
        if not os.path.isabs(normalized):
            raise DraftCheckError(
                "draft_check.evidence_root must be an absolute path"
            )
        return {
            "enabled": True,
            "jev_live": True,
            "evidence_root": normalized,
        }
    if evidence_root is not None:
        raise DraftCheckError(
            "draft_check.evidence_root requires jev_live true"
        )
    return {"enabled": True}
