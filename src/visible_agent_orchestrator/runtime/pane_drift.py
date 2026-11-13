"""Safe decisions for automatic visible-worker pane drift repair."""

from __future__ import annotations

from typing import Any


AUTO_REBIND_STATUSES = {
    "rebind_available",
    "stale_pane_rebind_available",
    "binding_lease_rebind_available",
    "registry_identity_mismatch_rebind_available",
}


def _terminal_lock(lock: dict[str, Any]) -> bool:
    truth = lock.get("task_truth", {}) if isinstance(lock.get("task_truth"), dict) else {}
    diagnosis = str(lock.get("diagnosis") or "").strip()
    lock_state = str(truth.get("lock_state") or "").strip()
    protocol_state = str(lock.get("protocol_terminal_state") or truth.get("protocol_terminal_state") or "").strip()
    status = str(lock.get("status") or "").strip()
    return (
        bool(lock.get("terminal"))
        or bool(truth.get("lock_is_terminal_stale"))
        or diagnosis in {"terminal_stale_lock", "submit_failed_lock"}
        or lock_state == "stale_terminal_lock"
        or protocol_state in {"completed", "blocked", "failed", "submit_failed"}
        or status in {"completed", "blocked", "failed", "submit_failed"}
    )


def _worker_has_active_lock(locks_payload: dict[str, Any], worker_id: str) -> bool:
    locks = locks_payload.get("locks", []) if isinstance(locks_payload, dict) else []
    if not isinstance(locks, list):
        return False
    for item in locks:
        if not isinstance(item, dict):
            continue
        if str(item.get("worker_id") or "") != worker_id:
            continue
        if not _terminal_lock(item):
            return True
    return False


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except Exception:
        return 0
    return parsed if parsed > 0 else 0


def evaluate_auto_rebind_decision(
    rebind_check: dict[str, Any],
    locks_payload: dict[str, Any],
    *,
    backend: str,
) -> dict[str, Any]:
    """Return a fail-closed decision for automatic pane-drift rebind.

    The stable identity is the worker id plus OpenCode port. Pane ids are tmux
    runtime snapshots, so a strong port-discovered live pane can safely refresh
    registry metadata when no active owner lock exists.
    """

    worker_id = str(rebind_check.get("worker_id") or "").strip()
    if backend != "tmux-reference":
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "auto rebind is limited to tmux-reference"}
    if not worker_id:
        return {"allowed": False, "action": "none", "worker_id": "", "reason": "missing worker_id"}
    status = str(rebind_check.get("status") or "").strip()
    if status == "current_binding_ok":
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "binding already current"}
    if status not in AUTO_REBIND_STATUSES:
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": f"status is not auto-rebindable: {status or '-'}"}
    if _worker_has_active_lock(locks_payload, worker_id):
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": f"worker {worker_id} has an active lock"}

    registry = rebind_check.get("registry", {}) if isinstance(rebind_check.get("registry"), dict) else {}
    live = rebind_check.get("live", {}) if isinstance(rebind_check.get("live"), dict) else {}
    checks = live.get("checks", {}) if isinstance(live.get("checks"), dict) else {}
    registry_port = _positive_int(registry.get("port"))
    live_port = _positive_int(live.get("port"))
    live_pane = str(live.get("pane_id") or "").strip()
    registry_pane = str(registry.get("pane_id") or "").strip()

    if str(live.get("binding_confidence") or "") != "strong":
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "live binding is not strong"}
    if not bool(checks.get("port_live")):
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "live port is not confirmed"}
    if not bool(checks.get("required_tui_present")):
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "required TUI endpoints are not confirmed"}
    if not live_pane:
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "live pane is missing"}
    if not registry_port or not live_port or registry_port != live_port:
        return {"allowed": False, "action": "none", "worker_id": worker_id, "reason": "registry and live ports do not match"}

    return {
        "allowed": True,
        "action": "rebind",
        "worker_id": worker_id,
        "port": registry_port,
        "registry_pane_id": registry_pane,
        "live_pane_id": live_pane,
        "reason": "strong live port binding can refresh runtime pane snapshot",
    }
