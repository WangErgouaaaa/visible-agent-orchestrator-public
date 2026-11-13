from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
SRC = ROOT / "src"
for path in (str(BIN), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_script(name: str, *, module_name: str | None = None):
    loader = importlib.machinery.SourceFileLoader(module_name or name, str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def root_defaults(parser: argparse.ArgumentParser) -> dict[str, object]:
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return {
        command: next(
            action.default
            for action in command_parser._actions
            if action.dest == "root"
        )
        for command, command_parser in subparsers.choices.items()
        if any(action.dest == "root" for action in command_parser._actions)
    }


RUNTIME_ENTRYPOINTS = (
    "omx_langgraph_control_plane",
    "omx_opencode_tui_adapter",
    "omx_task_signal",
    "omx_team_dispatch",
    "omx_visible_health",
    "omx_visible_orchestrate",
    "omx_visible_policy_check",
    "omx_visible_rebind",
    "omx_visible_recovery",
    "omx_visible_watcher",
)

MEETING_PARSERS = (
    "build_run_parser",
    "build_bridge_parser",
    "build_shared_transcript_parser",
    "build_worker_group_review_parser",
    "build_ag2_import_gate_parser",
    "build_ag2_status_parser",
    "build_ag2_release_readiness_parser",
    "build_ag2_runtime_preflight_parser",
    "build_ag2_run_parser",
    "build_ag2_adapter_plan_parser",
    "build_ag2_approval_status_parser",
    "build_ag2_approval_validate_parser",
    "build_ag2_host_local_config_status_parser",
    "build_ag2_host_local_config_validate_parser",
    "build_safety_check_parser",
)


def test_runtime_clis_use_vao_root_when_root_is_omitted(tmp_path: Path) -> None:
    dispatch = load_script("omx_visible_dispatch")
    launcher = load_script("omx_opencode_visible_launcher")
    first = tmp_path / "runtime one"
    second = tmp_path / "runtime two"

    assert set(root_defaults(dispatch.build_parser()).values()) == {None}
    assert set(root_defaults(launcher.build_parser()).values()) == {None}

    with patch.dict(os.environ, {"VAO_ROOT": str(first)}, clear=False):
        dispatch_first = dispatch.root_path(None)
        launcher_first = launcher.root_path(None)
    with patch.dict(os.environ, {"VAO_ROOT": str(second)}, clear=False):
        dispatch_second = dispatch.root_path(None)
        launcher_second = launcher.root_path(None)

    assert dispatch_first == launcher_first == first.resolve()
    assert dispatch_second == launcher_second == second.resolve()
    assert dispatch.framework_root(dispatch_first).is_relative_to(first)
    assert dispatch.framework_root(dispatch_second).is_relative_to(second)
    assert dispatch_first != dispatch_second


def test_explicit_runtime_root_wins_including_dot_and_spaces(tmp_path: Path) -> None:
    dispatch = load_script(
        "omx_visible_dispatch", module_name="omx_visible_dispatch_explicit"
    )
    launcher = load_script(
        "omx_opencode_visible_launcher",
        module_name="omx_opencode_visible_launcher_explicit",
    )
    explicit = tmp_path / "explicit runtime with spaces"

    with patch.dict(os.environ, {"VAO_ROOT": str(tmp_path / "ignored")}, clear=False):
        assert dispatch.root_path(str(explicit)) == explicit.resolve()
        assert launcher.root_path(str(explicit)) == explicit.resolve()
        assert dispatch.root_path(".") == ROOT.resolve()
        assert launcher.root_path(".") == ROOT.resolve()


def test_meeting_uses_workspace_root_and_preserves_argument_boundaries(
    tmp_path: Path,
) -> None:
    meeting = load_script("omx_ag2_meeting")
    selected = tmp_path / "workspace with spaces"
    args = meeting.build_parser().parse_args(
        ["--task", "review this", "--preset", "idea-review"]
    )
    assert args.root is None

    completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
    with (
        patch.dict(
            os.environ,
            {"VAO_WORKSPACE_ROOT": str(selected), "VAO_ROOT": str(tmp_path / "runtime")},
            clear=False,
        ),
        patch.object(meeting.subprocess, "run", return_value=completed) as run,
    ):
        meeting.run_delegated_ag2_run(args, None)

    command = run.call_args.args[0]
    root_index = command.index("--root") + 1
    assert command[root_index] == str(selected.resolve())
    assert command[root_index + 1 : root_index + 3] == ["--task", "review this"]


def test_meeting_explicit_workspace_root_wins(tmp_path: Path) -> None:
    meeting = load_script("omx_ag2_meeting", module_name="omx_ag2_meeting_explicit")
    explicit = tmp_path / "explicit workspace with spaces"
    args = meeting.build_parser().parse_args(
        [
            "--root",
            str(explicit),
            "--task",
            "review",
            "--preset",
            "idea-review",
        ]
    )

    with patch.dict(
        os.environ, {"VAO_WORKSPACE_ROOT": str(tmp_path / "ignored")}, clear=False
    ):
        assert meeting.workspace_root(args.root) == explicit.resolve()


def test_remaining_runtime_entrypoints_use_vao_root(tmp_path: Path) -> None:
    first = tmp_path / "runtime followup one"
    second = tmp_path / "runtime followup two"
    modules = [
        load_script(name, module_name=f"{name}_root_default_test")
        for name in RUNTIME_ENTRYPOINTS
    ]

    for module in modules:
        assert set(root_defaults(module.build_parser()).values()) == {None}
        with patch.dict(os.environ, {"VAO_ROOT": str(first)}, clear=False):
            first_root = module.root_path(None)
        with patch.dict(os.environ, {"VAO_ROOT": str(second)}, clear=False):
            second_root = module.root_path(None)
        assert first_root == first.resolve()
        assert second_root == second.resolve()
        assert first_root != second_root
        with patch.dict(os.environ, {"VAO_ROOT": str(first)}, clear=False):
            assert module.root_path(".") == ROOT.resolve()


def test_task_state_uses_runtime_root_default(tmp_path: Path) -> None:
    module = load_script(
        "omx_visible_task_state.py", module_name="omx_visible_task_state_root_default_test"
    )
    parser = module.build_parser()
    args = parser.parse_args(["--run", "run-001", "--task", "task-001"])
    selected = tmp_path / "task state root with spaces"

    assert args.root is None
    with patch.dict(os.environ, {"VAO_ROOT": str(selected)}, clear=False):
        assert module.root_path(args.root) == selected.resolve()
        assert module.root_path(".") == ROOT.resolve()


def test_workspace_entrypoints_use_workspace_root(tmp_path: Path) -> None:
    worker_launcher = load_script(
        "omx_opencode_worker_launcher",
        module_name="omx_opencode_worker_launcher_root_default_test",
    )
    sidebar = load_script(
        "omx_subagent_sidebar", module_name="omx_subagent_sidebar_root_default_test"
    )
    meeting_plan = load_script(
        "omx_meeting_plan", module_name="omx_meeting_plan_root_default_test"
    )
    selected = tmp_path / "workspace followup with spaces"

    assert set(root_defaults(worker_launcher.build_parser()).values()) == {None}
    sidebar_args = sidebar.build_parser().parse_args([])
    assert sidebar_args.root is None
    for parser_name in MEETING_PARSERS:
        parser = getattr(meeting_plan, parser_name)()
        root_action = next(action for action in parser._actions if action.dest == "root")
        assert root_action.default is None, parser_name

    with patch.dict(
        os.environ,
        {"VAO_WORKSPACE_ROOT": str(selected), "VAO_ROOT": str(tmp_path / "ignored")},
        clear=False,
    ):
        assert worker_launcher.root_path(None) == selected.resolve()
        assert sidebar.workspace_root(sidebar_args.root) == selected.resolve()
        assert meeting_plan.workspace_root(None) == selected.resolve()
        assert worker_launcher.root_path(".") == ROOT.resolve()
