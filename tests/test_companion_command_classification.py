from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

CLI_NAMES = (
    "omx_install_sync",
    "omx_visible_dispatch",
    "omx_visible_rebind",
    "omx_opencode_visible_launcher",
    "omx_opencode_tui_adapter",
    "omx_task_signal",
    "omx_langgraph_control_plane",
    "omx_visible_orchestrate",
    "omx_visible_recovery",
)
POLICIES = {"read-only", "shared-admission", "exclusive-admission", "recovery-only"}


def _parser_commands(cli_name: str) -> set[str]:
    namespace = runpy.run_path(str(ROOT / "bin" / cli_name))
    parser = namespace["build_parser"]()
    action = next(
        item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_registry_classifies_every_parser_command_bidirectionally() -> None:
    payload = json.loads(
        (ROOT / "contracts/companion-skill-mutation-surfaces.json").read_text(
            encoding="utf-8"
        )
    )
    commands = payload["commands"]
    parser_registry = {
        identity
        for identity in commands
        if identity != "omx_opencode_tui_adapter:verify-visible:update-registry"
    }
    parser_commands = {
        f"{cli}:{command}" for cli in CLI_NAMES for command in _parser_commands(cli)
    }

    assert parser_registry == parser_commands
    assert set(commands.values()) <= POLICIES


@pytest.mark.parametrize(
    ("identity", "policy"),
    [
        ("omx_visible_dispatch:collect", "shared-admission"),
        ("omx_visible_dispatch:wait", "shared-admission"),
        ("omx_install_sync:apply", "exclusive-admission"),
        ("omx_install_sync:companion-release-activate", "exclusive-admission"),
        ("omx_install_sync:companion-release-reconcile", "recovery-only"),
        ("omx_install_sync:rollback", "exclusive-admission"),
    ],
)
def test_reviewed_mutation_policies(identity: str, policy: str) -> None:
    payload = json.loads(
        (ROOT / "contracts/companion-skill-mutation-surfaces.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["commands"][identity] == policy
