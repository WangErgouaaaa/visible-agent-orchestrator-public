"""OpenCode TUI submit planning built on RuntimeAdapter worker evidence.

This module only builds plans. It does not POST to OpenCode, write protocol
markers, call rmux, or treat runtime readiness as protocol truth.
"""

from __future__ import annotations

from pathlib import Path

from visible_agent_orchestrator.runtime.backends import get_backend
from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError
from visible_agent_orchestrator.runtime.models import ResolveResult, RuntimeEvidence, SubmitPlan, VerifyResult


REQUIRED_TUI_API_PATHS = ["/tui/clear-prompt", "/tui/append-prompt", "/tui/submit-prompt"]


def validate_opencode_tui_worker_for_submit(
    resolve_result: ResolveResult,
    verify_result: VerifyResult,
) -> tuple[bool, str, list[str]]:
    """Return fail-closed submit eligibility for a resolved visible worker."""

    errors = list(resolve_result.errors) + list(verify_result.errors)
    warnings = list(resolve_result.warnings) + list(verify_result.warnings)
    if not resolve_result.dispatch_allowed:
        errors.append("runtime resolution dispatch_allowed=false")
    if not verify_result.ok or not verify_result.dispatch_allowed:
        errors.append("runtime binding verification dispatch_allowed=false")
    worker = resolve_result.worker
    if worker.role in {"test", "disabled"}:
        errors.append(f"worker {worker.worker_id} is not dispatchable: role={worker.role}")
    if not worker.allow_auto_submit:
        errors.append(f"worker {worker.worker_id} has allow_auto_submit=false")
    if worker.binding_confidence != "strong":
        errors.append(f"worker {worker.worker_id} binding_confidence is not strong: {worker.binding_confidence or '-'}")
    return not errors, "; ".join(dict.fromkeys(errors)), warnings


def build_opencode_tui_submit_plan(
    root: Path,
    worker_id: str,
    *,
    backend: str = "tmux-reference",
    fixture_dir: Path | None = None,
) -> SubmitPlan:
    """Build a machine-readable OpenCode TUI submit plan without side effects."""

    requested_worker = str(worker_id or "").strip()
    adapter = get_backend(backend, fixture_dir=fixture_dir)
    capability = adapter.submit_capability()
    backend_type = "rmux" if backend.startswith("rmux") else "tmux"
    common_actions = ["generate_submit_plan", "no_http_post", "no_marker_write"]

    if backend == "rmux-spike":
        plan = adapter.submit_plan(root, requested_worker)
        return SubmitPlan(
            worker_id=requested_worker,
            backend_name=backend,
            backend_type=backend_type,
            dispatch_allowed=False,
            submit_supported=False,
            submit_endpoint_kind="unsupported",
            planned_actions=list(plan.planned_actions or common_actions),
            blocked_reason="rmux live submit not implemented; rmux-spike is fixture-only",
            warnings=list(plan.warnings),
            evidence=list(plan.evidence),
        )

    try:
        resolved = adapter.resolve_worker(root, requested_worker)
        verified = adapter.verify_binding(root, requested_worker)
    except RuntimeAdapterError as exc:
        return SubmitPlan(
            worker_id=requested_worker,
            backend_name=backend,
            backend_type=backend_type,
            dispatch_allowed=False,
            submit_supported=False,
            submit_endpoint_kind="opencode_tui_api" if "opencode_tui_api" in capability.allowed_endpoint_kinds else "unsupported",
            required_api_paths=list(REQUIRED_TUI_API_PATHS),
            planned_actions=common_actions,
            blocked_reason=str(exc),
            evidence=[RuntimeEvidence("runtime-adapter", {"error": str(exc)})],
        )

    allowed, blocked_reason, warnings = validate_opencode_tui_worker_for_submit(resolved, verified)
    binding = resolved.binding
    endpoint_kind = "opencode_tui_api" if "opencode_tui_api" in capability.allowed_endpoint_kinds else "unsupported"
    return SubmitPlan(
        worker_id=resolved.worker.worker_id,
        backend_name=backend,
        backend_type=backend_type,
        dispatch_allowed=allowed,
        submit_supported=bool(allowed and capability.supports_submit_plan and endpoint_kind == "opencode_tui_api"),
        submit_endpoint_kind=endpoint_kind,
        target_host=binding.host,
        target_port=binding.port,
        target_pane=binding.pane_id,
        required_api_paths=list(REQUIRED_TUI_API_PATHS),
        planned_actions=common_actions + ["defer_live_submit_to_existing_adapter"],
        blocked_reason=blocked_reason,
        warnings=warnings,
        evidence=[
            RuntimeEvidence("runtime-adapter", {"backend": backend, "worker_id": resolved.worker.worker_id}),
            RuntimeEvidence("submit-plan", {"protocol_truth": "report.md plus exactly one terminal marker"}),
        ],
    )
