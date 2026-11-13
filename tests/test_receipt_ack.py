from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visible_agent_orchestrator.receipt_ack import ReceiptAckError, validate_receipt_ack

PROMPT_SHA = "7a38bf81f383f69433ad6e900f92ca948e15fe76b1b115ba589d4d53c582eb9f"


def make_task(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    run = "visible-run-fixture-receipt"
    task = "task-001"
    task_dir = (
        tmp_path
        / ".omx/framework/team/visible-runs"
        / run
        / "tasks"
        / task
    )
    task_dir.mkdir(parents=True)
    (task_dir / ".submitted").write_text("submitted\n", encoding="utf-8")
    (task_dir / "delivery.json").write_text(
        json.dumps({"schema_version": 1, "prompt_sha256": PROMPT_SHA}) + "\n",
        encoding="utf-8",
    )
    ack = {
        "schema_version": 1,
        "ack_kind": "worker_receipt",
        "source": "opencode_worker_plugin",
        "worker_id": "vw-worker-1",
        "run": run,
        "task": task,
        "task_dir": f".omx/framework/team/visible-runs/{run}/tasks/{task}",
        "delivery_prompt_sha256": PROMPT_SHA,
        "received_at": "2026-01-01T00:00:05Z",
        "plugin": {"name": "omx-visible-receipt", "version": 1},
    }
    return tmp_path, task_dir, ack


def test_validate_receipt_ack_accepts_matching_worker_task_and_hash(tmp_path: Path) -> None:
    root, task_dir, ack = make_task(tmp_path)

    result = validate_receipt_ack(root, task_dir, "vw-worker-1", ack)

    assert result.worker_id == "vw-worker-1"
    assert result.run == "visible-run-fixture-receipt"
    assert result.task == "task-001"
    assert result.delivery_prompt_sha256 == PROMPT_SHA


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("worker_id", "vw-worker-2", "wrong_worker"),
        ("delivery_prompt_sha256", "0" * 64, "wrong_prompt_hash"),
        ("task_dir", "../outside", "unsafe_task_dir"),
    ],
)
def test_validate_receipt_ack_rejects_mismatched_identity(
    tmp_path: Path, field: str, value: str, error: str
) -> None:
    root, task_dir, ack = make_task(tmp_path)
    ack[field] = value

    with pytest.raises(ReceiptAckError, match=error):
        validate_receipt_ack(root, task_dir, "vw-worker-1", ack)


def test_validate_receipt_ack_requires_submitted_nonterminal_task(tmp_path: Path) -> None:
    root, task_dir, ack = make_task(tmp_path)
    (task_dir / ".submitted").unlink()
    with pytest.raises(ReceiptAckError, match="missing_submitted_marker"):
        validate_receipt_ack(root, task_dir, "vw-worker-1", ack)

    (task_dir / ".submitted").write_text("submitted\n", encoding="utf-8")
    (task_dir / "report.md").write_text("closed\n", encoding="utf-8")
    (task_dir / ".done").write_text("done\n", encoding="utf-8")
    with pytest.raises(ReceiptAckError, match="terminal_state_already_exists"):
        validate_receipt_ack(root, task_dir, "vw-worker-1", ack)


def test_validate_receipt_ack_rejects_conflicting_worker_locks(tmp_path: Path) -> None:
    root, task_dir, ack = make_task(tmp_path)
    lock_dir = root / ".omx/framework/team/visible-runs/_locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "vw-worker-2.lock.json").write_text(
        json.dumps(
            {
                "worker_id": "vw-worker-2",
                "run": ack["run"],
                "task": ack["task"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReceiptAckError, match="wrong_active_lock"):
        validate_receipt_ack(root, task_dir, "vw-worker-1", ack)
