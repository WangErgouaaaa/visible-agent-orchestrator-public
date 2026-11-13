"""Narrow bridge from meeting proposals to existing control-plane commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.control_plane_contract import CommandRunner, default_command_runner

from .models import ControlPlaneDryRunProposedAction, MeetingResult


@dataclass(frozen=True)
class BridgeAction:
    argv: list[str]
    result: dict[str, Any]
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "result": dict(self.result), "dry_run": self.dry_run}


def build_control_plane_dry_run(
    *,
    root: Path,
    workers: list[str],
    title: str,
    body: str,
    runner: CommandRunner = default_command_runner,
    timeout: int = 300,
    allow_real: bool = False,
) -> BridgeAction:
    if allow_real:
        raise ValueError("build_control_plane_dry_run cannot execute real worker submissions")
    if not workers:
        raise ValueError("at least one worker is required")

    argv = [
        str(root / "bin" / "omx_langgraph_control_plane"),
        "run",
        "--root",
        str(root),
    ]
    for worker in workers:
        argv.extend(["--worker", worker])
    argv.extend(["--title", title, "--body", body, "--timeout", str(timeout), "--dry-run", "--json"])
    _validate_high_level_argv(argv)
    result = runner(argv, root, timeout)
    return BridgeAction(argv=argv, result=result, dry_run=True)


def build_control_plane_dry_run_proposed_action_payload(
    *,
    workers: list[str] | tuple[str, ...],
    title: str,
    body: str,
    timeout: int = 300,
) -> dict[str, Any]:
    return ControlPlaneDryRunProposedAction(
        workers=tuple(workers),
        title=title,
        body=body,
        timeout=timeout,
    ).to_payload()


def attach_proposed_action(
    result: MeetingResult,
    action: dict[str, Any] | ControlPlaneDryRunProposedAction,
) -> MeetingResult:
    proposed_action = _validate_control_plane_dry_run_action(action)
    return MeetingResult(
        task=result.task,
        participants=list(result.participants),
        messages=list(result.messages),
        selector_decisions=list(result.selector_decisions),
        termination_reason=result.termination_reason,
        verdict=result.verdict,
        proposed_actions=[*result.proposed_actions, proposed_action.to_payload()],
        risks=list(result.risks),
    )


def build_control_plane_dry_run_from_result(
    *,
    root: Path,
    result: MeetingResult,
    action_index: int | None = None,
    runner: CommandRunner = default_command_runner,
) -> BridgeAction:
    action = _select_proposed_action(result.proposed_actions, action_index)
    return build_control_plane_dry_run_from_action(root=root, action=action, runner=runner)


def build_control_plane_dry_run_from_result_payload(
    *,
    root: Path,
    result_payload: dict[str, Any],
    action_index: int | None = None,
    runner: CommandRunner = default_command_runner,
) -> BridgeAction:
    actions = result_payload.get("proposed_actions")
    action = _select_proposed_action(actions, action_index)
    return build_control_plane_dry_run_from_action(root=root, action=action, runner=runner)


def build_control_plane_dry_run_from_action(
    *,
    root: Path,
    action: dict[str, Any] | ControlPlaneDryRunProposedAction,
    runner: CommandRunner = default_command_runner,
) -> BridgeAction:
    proposed_action = _validate_control_plane_dry_run_action(action)
    return build_control_plane_dry_run(
        root=root,
        workers=list(proposed_action.workers),
        title=proposed_action.title,
        body=proposed_action.body,
        runner=runner,
        timeout=proposed_action.timeout,
        allow_real=False,
    )


def _select_proposed_action(actions: Any, action_index: int | None) -> dict[str, Any]:
    if not isinstance(actions, list) or not actions:
        raise ValueError("meeting result has no proposed_actions")
    if action_index is None:
        if len(actions) != 1:
            raise ValueError("action_index is required when meeting result contains multiple proposed_actions")
        selected_index = 0
    else:
        if type(action_index) is not int or action_index < 0 or action_index >= len(actions):
            raise ValueError("action_index is out of range")
        selected_index = action_index
    action = actions[selected_index]
    if not isinstance(action, dict):
        raise ValueError("proposed action must be an object")
    return action


def _validate_control_plane_dry_run_action(
    action: dict[str, Any] | ControlPlaneDryRunProposedAction,
) -> ControlPlaneDryRunProposedAction:
    if isinstance(action, ControlPlaneDryRunProposedAction):
        return action
    return ControlPlaneDryRunProposedAction.from_payload(action)


def _validate_high_level_argv(argv: list[str]) -> None:
    joined = " ".join(argv)
    forbidden = [
        "send-keys",
        "paste-buffer",
        "/tui/",
        "omx_opencode_tui_adapter",
        "tmux",
        "rmux",
        "subprocess",
        "omx_task_signal",
        "report.md",
        ".done",
        ".fail",
        ".blocked",
        "--allow-real",
    ]
    hit = next((item for item in forbidden if item in joined), "")
    if hit:
        raise ValueError(f"forbidden low-level control surface in meeting bridge argv: {hit}")
