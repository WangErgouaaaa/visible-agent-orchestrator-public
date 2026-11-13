"""Validate explicit visible-worker session lifecycle decisions."""

from __future__ import annotations

from collections.abc import Sequence


SESSION_ACTIONS = {"baseline", "keep", "compact", "rotate", "recovery"}
SESSION_ROTATION_REASONS = {
    "manual_new",
    "role_isolation",
    "context_domain_changed",
    "contract_breaking_changed",
    "context_contaminated",
    "context_rollover",
    "recovery_restart",
    "worker_reuse",
}
REHYDRATION_PACKET_TYPES = {
    "continuation",
    "clean-room-review",
    "new-task",
    "recovery",
}


class SessionActionError(ValueError):
    """The requested worker session lifecycle is incomplete or unsafe."""


def resolve_session_actions(
    values: Sequence[str] | None,
    workers: Sequence[str],
    *,
    reason: str = "",
    rotation_reason: str = "",
    rehydration_packet_type: str = "",
    required: bool = True,
    allow_recovery: bool = False,
) -> dict[str, str]:
    selected = list(dict.fromkeys(str(worker).strip() for worker in workers if str(worker).strip()))
    raw_values = [str(value).strip() for value in values or () if str(value).strip()]
    if not raw_values:
        if required:
            raise SessionActionError(
                "session action is required for every selected worker: "
                + ", ".join(selected)
            )
        return {}

    actions: dict[str, str] = {}
    if len(selected) == 1 and len(raw_values) == 1 and "=" not in raw_values[0]:
        actions[selected[0]] = raw_values[0]
    else:
        for value in raw_values:
            if "=" not in value:
                raise SessionActionError(
                    "multiworker session actions must use worker=action"
                )
            worker_id, action = (part.strip() for part in value.split("=", 1))
            if not worker_id or not action:
                raise SessionActionError(
                    "session action mappings must use non-empty worker=action"
                )
            if worker_id in actions:
                raise SessionActionError(
                    f"duplicate session action for worker: {worker_id}"
                )
            actions[worker_id] = action

    invalid = sorted(
        f"{worker_id}={action}"
        for worker_id, action in actions.items()
        if action not in SESSION_ACTIONS
    )
    if invalid:
        raise SessionActionError(
            "invalid session action: "
            + ", ".join(invalid)
            + f"; expected one of {', '.join(sorted(SESSION_ACTIONS))}"
        )
    missing = [worker_id for worker_id in selected if worker_id not in actions]
    extra = [worker_id for worker_id in actions if worker_id not in selected]
    if missing:
        raise SessionActionError(
            "session action is missing for selected worker: " + ", ".join(missing)
        )
    if extra:
        raise SessionActionError(
            "session action names unselected worker: " + ", ".join(extra)
        )

    normalized_reason = str(reason or "").strip()
    normalized_rotation_reason = str(rotation_reason or "").strip()
    normalized_packet_type = str(rehydration_packet_type or "").strip()
    if not normalized_reason:
        raise SessionActionError("session action reason is required")
    if "recovery" in actions.values() and not allow_recovery:
        raise SessionActionError(
            "session_action recovery requires the sanctioned recovery workflow"
        )
    if "rotate" in actions.values():
        if normalized_rotation_reason not in SESSION_ROTATION_REASONS:
            raise SessionActionError(
                "rotation reason is required and must be one of: "
                + ", ".join(sorted(SESSION_ROTATION_REASONS))
            )
        if normalized_packet_type not in REHYDRATION_PACKET_TYPES:
            raise SessionActionError(
                "rehydration packet type is required and must be one of: "
                + ", ".join(sorted(REHYDRATION_PACKET_TYPES))
            )
        if normalized_reason != normalized_rotation_reason:
            raise SessionActionError(
                "session action reason must match rotation reason for rotate"
            )
    elif normalized_rotation_reason or normalized_packet_type:
        raise SessionActionError(
            "rotation metadata requires at least one session_action=rotate"
        )
    return actions
