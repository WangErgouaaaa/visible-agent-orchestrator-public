"""Read-only rebind check summaries through RuntimeAdapter metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backends import default_backend_name, get_backend, is_backend_live_enabled
from .errors import RuntimeAdapterError
from .health import utc_now
from .registry import validate_worker_for_dispatch
from .tmux_capabilities import command_support_report


def _evidence_dicts(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if hasattr(item, "to_dict"):
            out.append(item.to_dict())
        elif isinstance(item, dict):
            out.append(item)
    return out


def _required_tui_present_from_evidence(items: list[dict[str, Any]]) -> bool | None:
    for item in items:
        detail = item.get("detail", {}) if isinstance(item, dict) else {}
        if not isinstance(detail, dict):
            continue
        if "all_required_tui_present" in detail:
            return bool(detail.get("all_required_tui_present"))
    return None


def binding_lease_rebind_required(
    worker: dict[str, Any],
    live: dict[str, Any],
    policy_errors: list[str],
) -> bool:
    """Return whether sanctioned rebind is the only needed policy repair."""
    worker_id = str(worker.get("worker_id") or "")
    lease_error_prefix = f"worker {worker_id} binding lease expired at "
    if any(
        not str(error).startswith(lease_error_prefix)
        for error in policy_errors
    ):
        return False
    if not policy_errors:
        return False
    for field in (
        "host_boot_id",
        "tmux_server_pid",
        "pane_id",
        "pane_pid",
        "opencode_pid",
        "tmux_session",
        "tmux_window",
    ):
        registry_value = worker.get(field)
        live_value = live.get(field)
        if (
            not registry_value
            or not live_value
            or str(registry_value) != str(live_value)
        ):
            return False
    for field in (
        "pane_start_time_ticks",
        "opencode_start_time_ticks",
    ):
        registry_value = worker.get(field)
        live_value = live.get(field)
        if bool(registry_value) != bool(live_value):
            return False
        if registry_value and str(registry_value) != str(live_value):
            return False
    return True


def explain_rebind_plan(summary: dict[str, Any]) -> dict[str, Any]:
    backend = str(summary.get("runtime_backend") or "")
    if backend == "rmux-spike":
        return {
            "action": "none",
            "real_rebind_allowed": False,
            "reason": "rmux-spike is fixture-only; real rebind fails closed",
        }
    if summary.get("status") == "current_binding_ok":
        if not summary.get("ready_for_dispatch", True):
            return {
                "action": "review_dispatch_policy",
                "real_rebind_allowed": True,
                "reason": (
                    "physical binding is current but dispatch policy blocks "
                    "task submission"
                ),
            }
        return {
            "action": "none",
            "real_rebind_allowed": True,
            "reason": "current binding metadata is OK",
        }
    return {
        "action": "manual_explicit_rebind_required",
        "real_rebind_allowed": True,
        "reason": "real rebind still requires --allow-rebind and strong live evidence",
    }


def build_rebind_check_summary(
    root: Path,
    worker_id: str,
    backend: str = "tmux-reference",
    fixture_dir: Path | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    fixture = Path(fixture_dir).resolve() if fixture_dir else None
    if backend == "rmux-spike" and fixture is None:
        raise RuntimeAdapterError("rmux-spike fixture required; live rmux disabled in spike-1")
    adapter = get_backend(backend, fixture_dir=fixture)
    resolved = adapter.resolve_worker(root, worker_id)
    verify = adapter.verify_binding(root, worker_id)
    binding = verify.binding or resolved.binding
    substrate_evidence = _evidence_dicts(verify.evidence)
    registry_evidence = _evidence_dicts(binding.evidence)
    registry_raw = getattr(resolved.worker, "raw", {}) if hasattr(resolved, "worker") else {}
    raw_errors = list(verify.errors) + list(resolved.errors)
    policy = validate_worker_for_dispatch(
        registry_raw if isinstance(registry_raw, dict) else {}
    )
    policy_errors = [
        str(item)
        for item in policy.get("errors", [])
    ]
    policy_error_set = set(policy_errors)
    alias_conflicts = registry_raw.get("binding_alias_conflicts", [])
    alias_binding_errors = (
        [
            f"worker {worker_id} physical binding alias conflict: "
            + "; ".join(str(item) for item in alias_conflicts)
        ]
        if isinstance(alias_conflicts, list) and alias_conflicts
        else []
    )
    blocking_errors = [
        str(item)
        for item in raw_errors
        if str(item) not in policy_error_set
    ]
    blocking_errors = list(
        dict.fromkeys(blocking_errors + alias_binding_errors)
    )
    required_tui_present = _required_tui_present_from_evidence(substrate_evidence)
    if required_tui_present is None:
        detected_from_raw = registry_raw.get("detected_tui_methods", []) if isinstance(registry_raw, dict) else []
        if isinstance(detected_from_raw, list) and bool(registry_raw.get("doc_available")):
            required_tui_present = all(
                path in [str(item) for item in detected_from_raw]
                for path in ["/tui/clear-prompt", "/tui/append-prompt", "/tui/submit-prompt"]
            )
        else:
            required_tui_present = bool(verify.ok and backend in {"tmux-reference", "rmux-reference"})
    if required_tui_present:
        detected_tui_methods = registry_raw.get("detected_tui_methods", []) if isinstance(registry_raw, dict) else []
        if not isinstance(detected_tui_methods, list) or not detected_tui_methods:
            detected_tui_methods = ["/tui/clear-prompt", "/tui/append-prompt", "/tui/submit-prompt"]
    else:
        detected_tui_methods = []
    binding_ok = bool(binding.binding_confidence == "strong" and not blocking_errors)
    dispatch_allowed = bool(policy.get("dispatch_allowed"))
    lease_rebind_required = bool(
        binding_ok
        and binding_lease_rebind_required(
            registry_raw,
            {
                "pane_id": binding.pane_id,
                "pane_pid": binding.process_id,
            },
            policy_errors,
        )
    )
    policy_warnings: list[str] = []
    if policy_errors and not blocking_errors:
        if registry_raw.get("allow_auto_submit") is False:
            policy_warnings.append(
                "worker is currently disabled for auto-submit but "
                "binding evidence is current"
            )
        else:
            policy_warnings.append(
                "worker dispatch policy is blocked but binding "
                "evidence is current"
            )
    status = "current_binding_ok" if binding_ok else "binding_failed"
    if lease_rebind_required:
        status = "binding_lease_rebind_available"
        policy_warnings.append(
            "worker binding lease requires sanctioned rebind"
        )
    if backend == "rmux-spike":
        status = "candidate_fixture_only" if binding_ok else "candidate_binding_failed"
    summary = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "root": str(root),
        "worker_id": worker_id,
        "status": status,
        "runtime_backend": backend,
        "backend_type": "rmux" if backend.startswith("rmux") else "tmux",
        "default_backend": default_backend_name(),
        "rmux_default": str(default_backend_name()).startswith("rmux"),
        "live_enabled": is_backend_live_enabled(backend),
        "fixture_mode": bool(fixture),
        **command_support_report(),
        "binding_confidence": binding.binding_confidence,
        "registry": {
            "pane_id": binding.pane_id,
            "pane_pid": binding.process_id,
            "port": binding.port,
            "binding_confidence": binding.binding_confidence,
        },
        "live": {
            "pane_id": binding.pane_id,
            "pane_pid": binding.process_id,
            "opencode_pid": None,
            "port": binding.port,
            "binding_confidence": binding.binding_confidence,
            "checks": {
                "required_tui_present": required_tui_present,
                "runtime_adapter_verify_ok": bool(binding_ok),
            },
            "detected_tui_methods": detected_tui_methods,
            "discovery_path": "",
        },
        "registry_evidence": registry_evidence,
        "substrate_evidence": substrate_evidence,
        "warnings": (
            list(verify.warnings)
            + list(resolved.warnings)
            + policy_warnings
        ),
        "errors": blocking_errors,
        "dispatch_policy_errors": policy_errors,
        "dispatch_policy_status": (
            "allowed" if dispatch_allowed else "blocked"
        ),
        "ready_for_dispatch": bool(
            status == "current_binding_ok" and dispatch_allowed
        ),
        "target_worker_rebind_available": lease_rebind_required,
        "ok": bool(binding_ok),
        "real_rebind_allowed": backend != "rmux-spike",
        "check_path": "",
    }
    summary["rebind_plan"] = explain_rebind_plan(summary)
    return summary
