"""Leader-facing orchestration entry helpers for visible OpenCode workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import shlex
import subprocess

from visible_agent_orchestrator.runtime.errors import RegistryError
from visible_agent_orchestrator.runtime.registry import (
    load_visible_workers,
    resolve_default_worker,
    resolve_worker_record,
    validate_worker_for_dispatch,
)
from visible_agent_orchestrator import task_feature_topology_policy as task_feature_policy
from visible_agent_orchestrator import topology_metadata as topology_contract
from visible_agent_orchestrator.lane_contracts import resolve_lane_assignments, write_lane_assignments
from visible_agent_orchestrator.session_actions import (
    SessionActionError,
    resolve_session_actions,
)


class LeaderOrchestrateError(Exception):
    """Operator-facing orchestration planning failure."""


@dataclass(frozen=True)
class OrchestrationPlan:
    root: Path
    route: str
    workers: list[str]
    backend: str
    argv: list[str]
    reason: str
    task_feature_profile: dict[str, Any]
    topology_recommendation: dict[str, Any]
    lane_assignments: list[dict[str, str]]
    lane_assignment_source: str
    lane_contracts_path: str
    lane_contracts_emitted: bool
    session_actions: dict[str, str]
    session_action_reason: str
    rotation_reason: str
    rehydration_packet_type: str


MEDIUM_FANOUT_HINTS = (
    "多智能体",
    "分工",
    "并发",
    "协作",
    "提提速",
    "提速",
    "快一点",
    "快点",
    "加快",
    "自己安排",
    "自己分工",
    "自己看着",
    "几个 agent",
    "几个 worker",
    "parallel",
    "multi-agent",
    "multiple agents",
    "multiple workers",
)
TOPOLOGY_TO_INNER_MODE = {
    "1x3": "4x3",
    "1x5": "4x7",
    "1x7": "4x7",
    "4x3": "4x3",
    "4x7": "4x7",
}
DEFAULT_VISIBLE_TIMEOUT_SECONDS = 300
DEFAULT_INNER_MODE_TIMEOUT_SECONDS = 900
STRONG_FANOUT_HINTS = (
    "能多开几路就多开几路",
    "尽量并发",
    "明显提速",
    "最大化利用 worker",
    "as many lanes as safe",
    "use as many workers as safe",
    "maximize worker parallelism",
)
CAUTIOUS_FANOUT_HINTS = (
    "别太冒险",
    "稳一点",
    "保守一点",
    "谨慎一点",
    "不要太激进",
    "不要冒进",
    "别太激进",
    "conservative",
    "be cautious",
    "do not be too aggressive",
)


def parse_workers(values: list[str]) -> list[str]:
    workers: list[str] = []
    for item in values:
        for raw in str(item).split(","):
            worker = raw.strip()
            if worker and worker not in workers:
                workers.append(worker)
    return workers


def selected_prompt_delivery_mode(
    task_contract: dict[str, Any] | None,
    *,
    explicit_full_prompt: bool = False,
) -> str:
    """The single prompt delivery mode selector (#904).

    The adapter's actual ``visible_prompt_submission`` selection and the
    leader's advisory plan/pre-submit output both resolve through this one
    helper so they cannot drift: full-prompt exposure (explicit flag or the
    contract's ``allow_visible_full_prompt``) selects ``visible-full-prompt``;
    otherwise a ``clean_room_startup`` contract selects
    ``clean-room-bootstrap-full-prompt``; everything else delivers the
    task-local hidden artifact envelope. Advisory callers explain the mode;
    only the adapter submits under it, and no permission is inferred here.
    """
    contract = task_contract if isinstance(task_contract, dict) else {}
    exposure = explicit_full_prompt or contract.get("allow_visible_full_prompt") is True
    if exposure:
        return "visible-full-prompt"
    if isinstance(contract.get("clean_room_startup"), dict):
        return "clean-room-bootstrap-full-prompt"
    return "hidden-artifact-envelope"


def prompt_delivery_scope_notice_lines(mode: str) -> list[str]:
    """Shared pre-submit delivery-scope explanation lines (#904).

    One wording source for the orchestrate run notice and the dispatch
    run-one pre-submit output. Advisory explanation of the deterministically
    selected mode only; no permission is inferred from task prose.
    """
    lines = [f"prompt_delivery_mode: {mode}"]
    if mode == "hidden-artifact-envelope":
        lines.append(
            "delivery_scope: hidden envelope is task-local-only - the worker "
            "cannot write the project tree; deliverables live inside the "
            "task directory and report.md references them concisely "
            "(path, scope, verification result) unless the exact task "
            "contract requires inline content; referenced files never "
            "expand this scope"
        )
        lines.append(
            "delivery_scope_remedy: when outside-task writes are already "
            "authorized, select full-prompt delivery before submission "
            "(--allow-visible-full-prompt or a prepared contract selecting "
            "it) with the exact authorized paths/actions stated literally "
            "in the task body; an already-submitted hidden task keeps its "
            "formal closeout/continuation path instead"
        )
    else:
        lines.append(
            "delivery_scope: full-prompt delivery - write authority is "
            "exactly the literal path/action grants written in the task "
            "body; this selection alone grants no additional writes"
        )
    return lines


def suggest_fanout_from_intent(title: str, body: str) -> int | None:
    haystack = f"{title}\n{body}".lower()
    if any(hint.lower() in haystack for hint in STRONG_FANOUT_HINTS):
        return 3
    if any(hint.lower() in haystack for hint in CAUTIOUS_FANOUT_HINTS) and any(
        hint.lower() in haystack for hint in MEDIUM_FANOUT_HINTS
    ):
        return 2
    if any(hint.lower() in haystack for hint in MEDIUM_FANOUT_HINTS):
        return 2
    return None


def _dispatchable_workers(root: Path) -> list[dict[str, Any]]:
    try:
        registry_workers = load_visible_workers(root)
    except RegistryError as exc:
        raise LeaderOrchestrateError(str(exc)) from exc
    dispatchable: list[dict[str, Any]] = []
    for record in registry_workers:
        checked = validate_worker_for_dispatch(record)
        if checked["dispatch_allowed"]:
            dispatchable.append(checked["worker"])
    defaults = [worker for worker in dispatchable if bool(worker.get("default_for_new_tasks"))]
    non_defaults = [worker for worker in dispatchable if not bool(worker.get("default_for_new_tasks"))]
    return defaults + non_defaults


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _current_worker_alert_counts(root: Path) -> dict[str, int]:
    alerts_path = root / ".omx" / "framework" / "team" / "visible-runs" / "_watcher" / "current_alerts.json"
    data = _read_json(alerts_path)
    alerts = data.get("alerts", []) if isinstance(data.get("alerts"), list) else []
    counts: dict[str, int] = {}
    for item in alerts:
        if not isinstance(item, dict):
            continue
        worker_id = str(item.get("worker_id") or item.get("worker") or "")
        if not worker_id:
            continue
        counts[worker_id] = counts.get(worker_id, 0) + 1
    return counts


def _current_active_lock_workers(root: Path) -> set[str]:
    lock_root = root / ".omx" / "framework" / "team" / "visible-runs" / "_locks"
    if not lock_root.is_dir():
        return set()
    active: set[str] = set()
    for path in sorted(lock_root.glob("*.lock.json")):
        data = _read_json(path)
        worker_id = str(data.get("worker_id") or "")
        truth = data.get("task_truth", {}) if isinstance(data.get("task_truth"), dict) else {}
        lock_state = str(truth.get("lock_state") or "").strip()
        terminal_state = str(truth.get("protocol_terminal_state") or data.get("protocol_terminal_state") or "").strip()
        status = str(data.get("status") or "").strip()
        terminal = bool(data.get("terminal")) or bool(truth.get("lock_is_terminal_stale"))
        terminal = terminal or lock_state == "stale_terminal_lock"
        terminal = terminal or terminal_state in {"completed", "blocked", "failed", "submit_failed"}
        terminal = terminal or status in {"completed", "blocked", "failed", "submit_failed"}
        # Owner lock files are intentionally lightweight; task_truth is often
        # computed by the lock doctor rather than persisted in the lock JSON.
        # Fail closed: a lock file without terminal evidence still owns the
        # worker and must block new fanout selection.
        if worker_id and not terminal and lock_state != "none":
            active.add(worker_id)
    return active


def _parse_key_value_lines(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        parsed[key] = value.strip()
    return parsed


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _infer_requested_topology(metadata: dict[str, Any], roles: list[str]) -> str:
    for key in ("recommended_inner_topology", "recommended_topology", "inner_topology", "inner_mode"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    if not roles:
        return ""
    if len(roles) <= 3:
        return "1x3"
    if len(roles) <= 5:
        return "1x5"
    return "1x7"


def resolve_topology_metadata(path_value: str | None) -> dict[str, Any]:
    try:
        return topology_contract.resolve_topology_metadata(path_value)
    except topology_contract.TopologyMetadataError as exc:
        raise LeaderOrchestrateError(str(exc)) from exc


def render_topology_metadata_context(metadata: dict[str, Any]) -> str:
    return topology_contract.render_topology_metadata_context(metadata)


def resolve_task_feature_profile(path_value: str | Path | None) -> dict[str, Any]:
    try:
        return task_feature_policy.resolve_task_feature_profile(path_value)
    except task_feature_policy.TaskFeatureTopologyPolicyError as exc:
        raise LeaderOrchestrateError(str(exc)) from exc


def _render_task_policy_reason(
    recommendation: dict[str, Any],
    overridden_by: str = "",
) -> str:
    reasons = ",".join(str(item) for item in recommendation.get("reasons", []) if str(item))
    parts = [
        f"task_policy=v{recommendation.get('policy_version')}",
        f"fanout={recommendation.get('recommended_fanout')}",
        f"confidence={recommendation.get('confidence')}",
        f"reasons={reasons}",
    ]
    if overridden_by:
        parts.append(f"overridden_by={overridden_by}")
    return "; ".join(parts)


def _live_worker_health(
    root: Path,
    worker_id: str,
    *,
    backend: str = "",
    require_live_verdict: bool = False,
) -> dict[str, Any]:
    health_bin = root / "bin" / "omx_visible_health"
    if not health_bin.is_file():
        if require_live_verdict:
            return {
                "returncode": 1,
                "overall": "ERROR",
                "rebind": "missing_health_checker",
                "active_locks": 0,
                "active_alerts": 0,
                "tui_readiness": "unknown",
                "post_terminal_continuation_state": "",
            }
        return {
            "returncode": 0,
            "overall": "OK",
            "rebind": "current_binding_ok",
            "active_locks": 0,
            "active_alerts": 0,
            "tui_readiness": "unknown",
            "post_terminal_continuation_state": "",
        }
    command = [
        str(health_bin),
        "check",
        "--root",
        str(root),
        "--worker",
        worker_id,
    ]
    if backend:
        command.extend(["--runtime-backend", backend])
    proc = subprocess.run(
        command,
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    parsed = _parse_key_value_lines(proc.stdout)
    stdout = proc.stdout or ""
    return {
        "returncode": int(proc.returncode),
        "overall": str(parsed.get("overall") or ""),
        "rebind": str(parsed.get("rebind") or ""),
        "active_locks": int(parsed.get("active_locks") or "0"),
        "active_alerts": int(parsed.get("active_alerts") or "0"),
        "tui_readiness": str(parsed.get("tui_readiness") or ""),
        "tui_readiness_reason": str(parsed.get("tui_readiness_reason") or ""),
        "post_terminal_continuation_state": str(parsed.get("post_terminal_continuation_state") or ""),
        "submit_safe": str(parsed.get("submit_safe") or "").lower() == "true",
        "terminal_stale_locks": (
            "terminal stale lock(s)" in stdout
            or "terminal stale locks present" in stdout
        ),
    }


def _split_ready_workers(root: Path, workers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    alert_counts = _current_worker_alert_counts(root)
    active_locks = _current_active_lock_workers(root)
    ready: list[dict[str, Any]] = []
    noisy: list[dict[str, Any]] = []
    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        live = _live_worker_health(root, worker_id) if worker_id else {}
        submit_safe = bool(live.get("submit_safe", True))
        readiness_reason = str(live.get("tui_readiness_reason") or "").lower()
        if (
            not submit_safe
            and str(live.get("tui_readiness") or "") == "unknown"
            and "advisory telemetry" in readiness_reason
            and "protocol markers remain authoritative" in readiness_reason
        ):
            submit_safe = True
        live_ready = bool(
            worker_id
            and int(live.get("returncode", 1)) == 0
            and str(live.get("overall") or "") == "OK"
            and str(live.get("rebind") or "") == "current_binding_ok"
            and int(live.get("active_locks", 0) or 0) == 0
            and int(live.get("active_alerts", 0) or 0) == 0
            and submit_safe
        )
        if (
            worker_id
            and worker_id not in active_locks
            and int(alert_counts.get(worker_id, 0)) == 0
            and live_ready
        ):
            ready.append(worker)
        else:
            noisy.append(worker)
    return ready, noisy


def _prefer_ready_workers(root: Path, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ready, noisy = _split_ready_workers(root, workers)
    return ready + noisy


def recommended_auto_recover_workers(root: Path, worker_ids: list[str]) -> list[str]:
    candidates: list[str] = []
    for worker_id in worker_ids:
        live = _live_worker_health(root, worker_id)
        if (
            bool(live.get("terminal_stale_locks"))
            or (
                int(live.get("active_locks", 0) or 0) == 0
                and int(live.get("active_alerts", 0) or 0) > 0
            )
        ):
            candidates.append(worker_id)
    return candidates


def _not_dispatchable_message(
    *,
    prefix: str,
    root: Path,
    worker_id: str,
    errors: list[str],
) -> str:
    """Render a dispatch rejection, adding recovery guidance only when the
    registry dispatch-policy check reports a lone lease expiry.

    The guidance is scoped to that policy result and never claims the worker
    is otherwise free: live identity, target owner locks, and authorization
    are not assessed by the policy check and stay enforced by their own
    gates. Planning runs no live probe, so the expiry string alone never
    claims a verified identity or a safe rebind. Mixed policy errors keep
    the full error list with no repair hint so other blockers stay unmasked.
    """
    message = f"{prefix}: {'; '.join(errors) or 'unknown reason'}"
    if not worker_id or not errors:
        return message
    lease_error_prefix = f"worker {worker_id} binding lease expired at "
    if any(not str(error).startswith(lease_error_prefix) for error in errors):
        return message
    check_command = shlex.join(
        [
            str(root / "bin" / "omx_visible_rebind"),
            "check",
            "--root",
            str(root),
            "--worker",
            worker_id,
            "--json",
        ]
    )
    rebind_command = shlex.join(
        [
            str(root / "bin" / "omx_visible_rebind"),
            "rebind",
            "--root",
            str(root),
            "--worker",
            worker_id,
            "--allow-rebind",
        ]
    )
    return (
        f"{message}; registry dispatch policy reports only a binding-lease "
        "expiry; live identity, target locks, and authorization are "
        "not assessed here and stay enforced by their own gates; planning "
        "itself never repairs bindings, so confirm repairability read-only "
        f"with {check_command} and only when it reports "
        "status=binding_lease_rebind_available (live binding still matches "
        f"the registry) run the explicitly authorized {rebind_command}, "
        "which still enforces its own target-lock and identity gates, then "
        "retry this command"
    )


def choose_workers(
    root: Path,
    *,
    backend: str,
    requested_workers: list[str],
    all_production_workers: bool,
    fanout: int | None,
) -> list[dict[str, Any]]:
    if requested_workers:
        resolved: list[dict[str, Any]] = []
        for worker_id in requested_workers:
            try:
                record = resolve_worker_record(root, worker_id)
            except RegistryError as exc:
                raise LeaderOrchestrateError(str(exc)) from exc
            checked = validate_worker_for_dispatch(record)
            if not checked["dispatch_allowed"]:
                raise LeaderOrchestrateError(
                    _not_dispatchable_message(
                        prefix=f"worker {worker_id} is not dispatchable",
                        root=root,
                        worker_id=worker_id,
                        errors=[str(item) for item in checked["errors"]],
                    )
                )
            resolved.append(checked["worker"])
        return resolved

    if not all_production_workers and fanout in (None, 1):
        try:
            default_worker = resolve_default_worker(root)
        except RegistryError as exc:
            raise LeaderOrchestrateError(str(exc)) from exc
        checked = validate_worker_for_dispatch(
            default_worker,
            must_be_default=True,
        )
        if not checked["dispatch_allowed"]:
            raise LeaderOrchestrateError(
                _not_dispatchable_message(
                    prefix="default worker is not dispatchable",
                    root=root,
                    worker_id=str(checked["worker"].get("worker_id") or ""),
                    errors=[str(item) for item in checked["errors"]],
                )
            )
        live = _live_worker_health(
            root,
            str(checked["worker"]["worker_id"]),
            backend=backend,
            require_live_verdict=True,
        )
        if (
            int(live.get("returncode", 1)) != 0
            or str(live.get("rebind") or "") != "current_binding_ok"
        ):
            raise LeaderOrchestrateError(
                "no healthy default worker; pass --worker <healthy-worker-id>"
            )
        return [checked["worker"]]

    dispatchable = _dispatchable_workers(root)
    if not dispatchable:
        raise LeaderOrchestrateError("no dispatchable visible workers found in registry")
    ready_workers, noisy_workers = _split_ready_workers(root, dispatchable)
    ordered_dispatchable = ready_workers + noisy_workers

    if all_production_workers:
        if noisy_workers:
            blocked = ", ".join(str(worker.get("worker_id") or "") for worker in noisy_workers if worker.get("worker_id"))
            raise LeaderOrchestrateError(
                f"--all-production-workers requested, but some workers are not currently safe for dispatch: {blocked}"
            )
        return ordered_dispatchable

    if fanout is not None:
        if fanout < 1:
            raise LeaderOrchestrateError("--fanout must be >= 1")
        if fanout > len(dispatchable):
            raise LeaderOrchestrateError(
                f"requested fanout={fanout}, but only {len(dispatchable)} dispatchable workers are available"
            )
        if fanout > len(ready_workers):
            blocked = ", ".join(str(worker.get("worker_id") or "") for worker in noisy_workers if worker.get("worker_id"))
            raise LeaderOrchestrateError(
                f"requested fanout={fanout}, but only {len(ready_workers)} workers are currently safe for dispatch; blocked_workers={blocked or '-'}"
            )
        return ready_workers[:fanout]


def render_worker_template(template: str, worker_id: str) -> str:
    return str(template or "").replace("{worker_id}", worker_id)


def route_from_count(
    worker_count: int,
    *,
    require_confirmation: bool,
    force_langgraph: bool,
    inner_mode: str = "",
) -> tuple[str, str]:
    if force_langgraph:
        return "langgraph", "force_langgraph=true"
    if inner_mode:
        return "langgraph", f"inner_mode={inner_mode}"
    if require_confirmation:
        return "langgraph", "require_confirmation=true"
    if worker_count > 1:
        return "langgraph", f"worker_count={worker_count}"
    return "dispatch", "single dispatchable worker"


def resolve_routing_inputs(
    *,
    requested_workers: list[str],
    all_production_workers: bool,
    fanout: int | None,
    inner_mode: str,
    topology_metadata: str,
    task_profile: str | Path | None,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Shared pure, no-write computation of the routing inputs used by the
    planner and by input gates: resolved inner mode, desired fanout, and the
    policy override marker. Reads no registry and performs no dispatchability
    or health checks."""
    topology_hint = resolve_topology_metadata(topology_metadata)
    resolved_inner_mode = inner_mode or str(topology_hint.get("resolved_inner_mode") or "")
    task_feature_profile = resolve_task_feature_profile(task_profile)
    topology_recommendation = (
        task_feature_policy.recommend_topology(task_feature_profile)
        if task_feature_profile
        else {}
    )
    desired_fanout = fanout
    policy_overridden_by = ""
    if requested_workers:
        policy_overridden_by = "worker"
    elif all_production_workers:
        policy_overridden_by = "all_production_workers"
    elif fanout is not None:
        policy_overridden_by = "fanout"
    elif resolved_inner_mode:
        policy_overridden_by = "inner_mode" if inner_mode else "topology_metadata"
        desired_fanout = None
    elif topology_recommendation:
        desired_fanout = int(topology_recommendation["recommended_fanout"])
    else:
        desired_fanout = suggest_fanout_from_intent(title, body)
    return {
        "topology_hint": topology_hint,
        "resolved_inner_mode": resolved_inner_mode,
        "desired_fanout": desired_fanout,
        "policy_overridden_by": policy_overridden_by,
        "topology_recommendation": topology_recommendation,
        "task_feature_profile": task_feature_profile,
    }


def file_input_route(
    root: Path,
    *,
    requested_workers: list[str],
    all_production_workers: bool,
    fanout: int | None,
    inner_mode: str,
    topology_metadata: str,
    task_profile: str | Path | None,
    require_confirmation: bool,
    force_langgraph: bool,
    title: str,
    body: str,
) -> tuple[str, str]:
    """Pure route decision for a file-body input, without turning routing
    into a dispatchability gate for explicitly selected workers: no worker
    validation, health probing, or lane writes happen here, so an expired
    lease or other repairable binding still reaches the sanctioned repair
    path. Worker count for routing comes from the explicit request, the
    existing dispatchable candidate set for --all-production-workers (the
    same set that selection actually uses), or the shared desired fanout;
    failures those inputs would raise later abort the run rather than
    changing its route."""
    inputs = resolve_routing_inputs(
        requested_workers=requested_workers,
        all_production_workers=all_production_workers,
        fanout=fanout,
        inner_mode=inner_mode,
        topology_metadata=topology_metadata,
        task_profile=task_profile,
        title=title,
        body=body,
    )
    if requested_workers:
        worker_count = len(requested_workers)
    elif all_production_workers:
        worker_count = len(_dispatchable_workers(root))
    else:
        desired = inputs["desired_fanout"]
        worker_count = desired if isinstance(desired, int) and desired > 1 else 1
    return route_from_count(
        worker_count,
        require_confirmation=require_confirmation,
        force_langgraph=force_langgraph,
        inner_mode=str(inputs["resolved_inner_mode"]),
    )


def choose_route(*, workers: list[dict[str, Any]], require_confirmation: bool, force_langgraph: bool, inner_mode: str = "") -> tuple[str, str]:
    return route_from_count(
        len(workers),
        require_confirmation=require_confirmation,
        force_langgraph=force_langgraph,
        inner_mode=inner_mode,
    )


def build_run_plan(
    root: Path,
    *,
    backend: str,
    requested_workers: list[str],
    all_production_workers: bool,
    fanout: int | None,
    title: str,
    body: str,
    timeout: int,
    allow_real: bool,
    require_confirmation: bool,
    force_langgraph: bool,
    thread_id: str | None,
    approved: bool,
    json_output: bool,
    inner_mode: str = "",
    artifact_root: str = "",
    topology_metadata: str = "",
    lane_contracts: str = "",
    emit_lane_contracts: str = "",
    model: str = "",
    nominal_context_tokens: int = 0,
    effective_context_tokens: int = 0,
    task_profile: str | Path | None = None,
    session_action_values: list[str] | None = None,
    session_action_reason: str = "",
    rotation_reason: str = "",
    rehydration_packet_type: str = "",
) -> OrchestrationPlan:
    routing = resolve_routing_inputs(
        requested_workers=requested_workers,
        all_production_workers=all_production_workers,
        fanout=fanout,
        inner_mode=inner_mode,
        topology_metadata=topology_metadata,
        task_profile=task_profile,
        title=title,
        body=body,
    )
    topology_hint = routing["topology_hint"]
    resolved_inner_mode = routing["resolved_inner_mode"]
    topology_recommendation = routing["topology_recommendation"]
    task_feature_profile = routing["task_feature_profile"]
    desired_fanout = routing["desired_fanout"]
    policy_overridden_by = routing["policy_overridden_by"]
    effective_timeout = int(timeout)
    if resolved_inner_mode and effective_timeout == DEFAULT_VISIBLE_TIMEOUT_SECONDS:
        effective_timeout = DEFAULT_INNER_MODE_TIMEOUT_SECONDS
    workers = choose_workers(
        root,
        backend=backend,
        requested_workers=requested_workers,
        all_production_workers=all_production_workers,
        fanout=desired_fanout,
    )
    route, reason = choose_route(
        workers=workers,
        require_confirmation=require_confirmation,
        force_langgraph=force_langgraph,
        inner_mode=resolved_inner_mode,
    )
    reason_parts = [reason]
    if topology_recommendation:
        reason_parts = [
            _render_task_policy_reason(
                topology_recommendation,
                policy_overridden_by,
            )
        ]
    elif fanout is None and desired_fanout is not None and route == "langgraph":
        reason_parts = [f"intent_fanout={desired_fanout}"]
    if topology_hint:
        reason_parts.append(
            f"chapter_topology={topology_hint.get('requested_topology') or '-'}->{topology_hint.get('resolved_inner_mode') or '-'}"
        )
    reason = "; ".join(part for part in reason_parts if part)
    worker_ids = [str(item.get("worker_id") or "") for item in workers]
    try:
        session_actions = resolve_session_actions(
            session_action_values,
            worker_ids,
            reason=session_action_reason,
            rotation_reason=rotation_reason,
            rehydration_packet_type=rehydration_packet_type,
            required=allow_real,
        )
    except SessionActionError as exc:
        raise LeaderOrchestrateError(str(exc)) from exc
    explicit_lane_contracts = str(lane_contracts or "").strip()
    emitted_lane_contracts = str(emit_lane_contracts or "").strip()
    lane_assignments = resolve_lane_assignments(
        worker_ids,
        title=title,
        body=body,
        inner_mode=resolved_inner_mode,
        lane_contracts_path=explicit_lane_contracts,
    )
    lane_assignment_source = "none"
    effective_lane_contracts = explicit_lane_contracts
    lane_contracts_emitted = False
    if lane_assignments:
        if explicit_lane_contracts:
            lane_assignment_source = "explicit_file"
        elif emitted_lane_contracts:
            effective_lane_contracts = write_lane_assignments(emitted_lane_contracts, lane_assignments)
            lane_assignment_source = "auto_generated"
            lane_contracts_emitted = True
        else:
            lane_assignment_source = "heuristic"
    if route == "dispatch":
        argv = [
            str(root / "bin" / "omx_visible_dispatch"),
            "run-one",
            "--root",
            str(root),
            "--worker",
            worker_ids[0],
            "--title",
            render_worker_template(title, worker_ids[0]),
            "--body",
            render_worker_template(body, worker_ids[0]),
            "--timeout",
            str(timeout),
        ]
        if allow_real:
            argv.append("--allow-real")
        if session_actions:
            argv.extend(
                [
                    "--session-action",
                    session_actions[worker_ids[0]],
                    "--session-action-reason",
                    session_action_reason,
                ]
            )
            if session_actions[worker_ids[0]] == "rotate":
                argv.extend(
                    [
                        "--rotation-reason",
                        rotation_reason,
                        "--rehydration-packet-type",
                        rehydration_packet_type,
                    ]
                )
        return OrchestrationPlan(
            root=root,
            route=route,
            workers=worker_ids,
            backend=backend,
            argv=argv,
            reason=reason,
            task_feature_profile=task_feature_profile,
            topology_recommendation=topology_recommendation,
            lane_assignments=lane_assignments,
            lane_assignment_source=lane_assignment_source,
            lane_contracts_path=effective_lane_contracts,
            lane_contracts_emitted=lane_contracts_emitted,
            session_actions=session_actions,
            session_action_reason=session_action_reason,
            rotation_reason=rotation_reason,
            rehydration_packet_type=rehydration_packet_type,
        )

    body_value = body
    if topology_hint:
        body_value = f"{str(body_value or '').rstrip()}\n{render_topology_metadata_context(topology_hint)}\n"
    argv = [
        str(root / "bin" / "omx_langgraph_control_plane"),
        "run",
        "--root",
        str(root),
        "--backend",
        backend,
    ]
    for worker_id in worker_ids:
        argv.extend(["--worker", worker_id])
    argv.extend(["--title", title, "--body", body_value, "--timeout", str(effective_timeout)])
    if resolved_inner_mode:
        argv.extend(["--inner-mode", resolved_inner_mode])
    if artifact_root:
        argv.extend(["--artifact-root", artifact_root])
    if effective_lane_contracts:
        argv.extend(["--lane-contracts", effective_lane_contracts])
    if lane_assignment_source:
        argv.extend(["--lane-assignment-source", lane_assignment_source])
    if model:
        argv.extend(["--model", model])
    if int(nominal_context_tokens or 0) > 0:
        argv.extend(["--nominal-context-tokens", str(int(nominal_context_tokens))])
    if int(effective_context_tokens or 0) > 0:
        argv.extend(["--effective-context-tokens", str(int(effective_context_tokens))])
    if allow_real:
        argv.append("--allow-real")
    if require_confirmation:
        argv.append("--require-confirmation")
    if approved:
        argv.append("--approved")
    if thread_id:
        argv.extend(["--thread-id", thread_id])
    if json_output:
        argv.append("--json")
    if session_actions:
        for worker_id in worker_ids:
            argv.extend(
                [
                    "--session-action",
                    f"{worker_id}={session_actions[worker_id]}",
                ]
            )
        argv.extend(
            [
                "--session-action-reason",
                session_action_reason,
            ]
        )
        if "rotate" in session_actions.values():
            argv.extend(
                [
                    "--rotation-reason",
                    rotation_reason,
                    "--rehydration-packet-type",
                    rehydration_packet_type,
                ]
            )
    return OrchestrationPlan(
        root=root,
        route=route,
        workers=worker_ids,
        backend=backend,
        argv=argv,
        reason=reason,
        task_feature_profile=task_feature_profile,
        topology_recommendation=topology_recommendation,
        lane_assignments=lane_assignments,
        lane_assignment_source=lane_assignment_source,
        lane_contracts_path=effective_lane_contracts,
        lane_contracts_emitted=lane_contracts_emitted,
        session_actions=session_actions,
        session_action_reason=session_action_reason,
        rotation_reason=rotation_reason,
        rehydration_packet_type=rehydration_packet_type,
    )
