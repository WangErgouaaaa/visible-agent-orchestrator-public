from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from visible_agent_orchestrator.codex_inventory_hook import compact_command
from visible_agent_orchestrator.global_mutation_gate import (
    global_mutation_gate,
    global_mutation_gate_path,
)
from visible_agent_orchestrator.runtime_paths import runtime_root, workspace_root
from visible_agent_orchestrator.supervision_runtime import supervision_control_root


class RuntimePortabilityTests(unittest.TestCase):
    def test_explicit_root_wins_and_preserves_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vao explicit root ") as directory:
            explicit = Path(directory) / "runtime with spaces"
            selected = runtime_root(explicit, env={"VAO_ROOT": "/ignored/runtime"})

        self.assertEqual(selected, explicit.resolve())

    def test_defaults_are_portable_and_workspace_is_separate(self) -> None:
        home = Path("/tmp/portable-user-home")
        with patch("visible_agent_orchestrator.runtime_paths.Path.home", return_value=home):
            self.assertEqual(runtime_root(env={}), home / "vao-runtime")
            self.assertEqual(workspace_root(env={}), home / "vao-workspace")

    def test_inventory_hook_uses_selected_runtime_executable(self) -> None:
        selected = Path("/tmp/portable runtime")
        executable = selected / "bin" / "omx_visible_dispatch"
        command = shlex.join(
            [str(executable), "status", "--root", str(selected), "--json"]
        )

        self.assertEqual(
            compact_command(command, env={"VAO_ROOT": str(selected)}),
            command + " --compact",
        )
        self.assertIsNone(
            compact_command(command, env={"VAO_ROOT": "/tmp/other-runtime"})
        )

    def test_separate_roots_keep_locks_and_supervision_state_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vao root isolation ") as directory:
            base = Path(directory)
            first = base / "runtime one"
            second = base / "runtime two"

            with global_mutation_gate(first, exclusive=True):
                self.assertTrue(global_mutation_gate_path(first).is_file())
                self.assertFalse(global_mutation_gate_path(second).exists())
            with global_mutation_gate(second, exclusive=True):
                self.assertTrue(global_mutation_gate_path(second).is_file())

            self.assertNotEqual(
                supervision_control_root(first, "run-1"),
                supervision_control_root(second, "run-1"),
            )
            self.assertTrue(global_mutation_gate_path(first).is_relative_to(first))
            self.assertTrue(global_mutation_gate_path(second).is_relative_to(second))


if __name__ == "__main__":
    unittest.main()
