from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from terminal_protocol_fixtures import write_legacy_baseline, write_modern_task_records
from visible_agent_orchestrator.terminal_acceptance import terminal_acceptance_status
from visible_agent_orchestrator.terminal_protocol import classify_terminal_protocol


def test_current_task_fixture_emits_accepted_modern_protocol(tmp_path: Path) -> None:
    task_dir = tmp_path / ".omx/framework/team/visible-runs/run-001/tasks/task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "report.md").write_text("complete\n", encoding="utf-8")
    (task_dir / ".done").write_text("{}\n", encoding="utf-8")

    write_modern_task_records(
        task_dir,
        run_id="run-001",
        task_id="task-001",
        worker_id="vw-worker-1",
        attempt_id="attempt-001",
        contract_hash="contract-001",
        session_id="ses_fixture_001",
        instance_id="instance-fixture-001",
        binding_generation=1,
        fencing_generation=1,
        state="done",
    )

    assert classify_terminal_protocol(task_dir)["classification"] == "modern_v2"
    assert terminal_acceptance_status(task_dir)["accepted"] is True


def test_pre_v2_fixture_emits_sealed_legacy_membership(tmp_path: Path) -> None:
    task_dir = tmp_path / ".omx/framework/team/visible-runs/run-001/tasks/task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "report.md").write_text("complete\n", encoding="utf-8")
    (task_dir / "task.json").write_text(
        '{"id":"task-001","run":"run-001"}\n', encoding="utf-8"
    )

    write_legacy_baseline(task_dir)

    assert classify_terminal_protocol(task_dir)["classification"] == "legacy"
