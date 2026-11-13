"""Sanctioned control-plane adapter for the first supervisor live edge."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.supervision_policy import (
    SupervisionPolicyError,
    normalize_isolated_write_contract,
)

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class SupervisionDispatchError(RuntimeError):
    """Raised when a preauthorized readonly handoff cannot be submitted safely."""


Runner = Callable[[list[str]], dict[str, Any]]


def dispatch_readonly_handoff(
    root: Path | str,
    handoff: dict[str, Any],
    *,
    dispatch_path: Path | str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Create and submit one readonly task through the existing control plane."""
    return _dispatch_worker_handoff(
        root,
        handoff,
        dispatch_path=dispatch_path,
        expected_effect_class="read_only_worker",
        runner=runner,
    )


def dispatch_isolated_write_handoff(
    root: Path | str,
    handoff: dict[str, Any],
    *,
    dispatch_path: Path | str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Create one isolated-write draft through the existing control plane."""
    return _dispatch_worker_handoff(
        root,
        handoff,
        dispatch_path=dispatch_path,
        expected_effect_class="write_isolated",
        runner=runner,
    )


def _dispatch_worker_handoff(
    root: Path | str,
    handoff: dict[str, Any],
    *,
    dispatch_path: Path | str,
    expected_effect_class: str,
    runner: Runner | None,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    command_path = Path(dispatch_path)
    supplied_runner = runner is not None
    if runner is None:
        if not command_path.is_file():
            raise SupervisionDispatchError(
                f"visible dispatch command is unavailable: {command_path}"
            )
        runner = _subprocess_runner(root_path)

    normalized = _normalize_handoff(
        handoff,
        expected_effect_class=expected_effect_class,
    )
    if expected_effect_class == "write_isolated" and not supplied_runner:
        _verify_isolated_write_worktree(normalized["target"]["isolated_write"])
    target = normalized["target"]
    worker_id = target["worker_id"]
    command_prefix = [str(command_path)]

    preflight = preflight_readonly_worker(
        root_path,
        worker_id,
        dispatch_path=command_path,
        runner=runner,
    )
    if preflight != target["expected_binding"]:
        raise SupervisionDispatchError("worker binding drift after supervisor snapshot")

    task_id = _task_id(normalized["logical_action_id"])
    task_contract = _worker_task_contract(normalized)
    _run_checked(
        runner,
        command_prefix
        + [
            "new-task",
            "--root",
            str(root_path),
            "--run",
            normalized["run_id"],
            "--task",
            task_id,
            "--worker",
            worker_id,
            "--title",
            target["task_title"],
            "--body",
            target["task_body"],
            "--task-contract-json",
            json.dumps(task_contract, sort_keys=True, separators=(",", ":")),
        ],
        "task creation",
    )
    binding = target["expected_binding"]
    _run_checked(
        runner,
        command_prefix
        + [
            "submit",
            "--root",
            str(root_path),
            "--run",
            normalized["run_id"],
            "--task",
            task_id,
            "--worker",
            worker_id,
            "--expected-instance-id",
            binding["instance_id"],
            "--expected-binding-generation",
            str(binding["binding_generation"]),
            "--expected-fencing-generation",
            str(binding["fencing_generation"]),
            "--supervision-run-id",
            normalized["supervision"]["run_id"],
            "--supervision-logical-action-id",
            normalized["logical_action_id"],
            "--supervision-lease-epoch",
            str(normalized["supervision"]["lease_epoch"]),
            "--supervision-fencing-token",
            normalized["supervision"]["fencing_token"],
            "--allow-real",
        ],
        f"{expected_effect_class} worker dispatch",
    )
    return {
        "schema_version": 1,
        "receipt_id": normalized["logical_action_id"],
        "run_id": normalized["run_id"],
        "task_id": task_id,
        "worker_id": worker_id,
        "dispatch_path": str(command_path),
        "binding": binding,
        "task_contract_digest": _digest(task_contract),
    }


def lookup_readonly_handoff_receipt(
    root: Path | str,
    handoff: dict[str, Any],
) -> dict[str, Any] | None:
    """Read the durable task receipt for one readonly handoff, if present."""
    return _lookup_worker_handoff_receipt(
        root,
        handoff,
        expected_effect_class="read_only_worker",
    )


def lookup_isolated_write_handoff_receipt(
    root: Path | str,
    handoff: dict[str, Any],
) -> dict[str, Any] | None:
    """Read the durable task receipt for one isolated-write handoff."""
    return _lookup_worker_handoff_receipt(
        root,
        handoff,
        expected_effect_class="write_isolated",
    )


def _lookup_worker_handoff_receipt(
    root: Path | str,
    handoff: dict[str, Any],
    *,
    expected_effect_class: str,
) -> dict[str, Any] | None:
    root_path = Path(root).resolve()
    normalized = _normalize_handoff(
        handoff,
        expected_effect_class=expected_effect_class,
    )
    task_id = _task_id(normalized["logical_action_id"])
    task_dir = (
        root_path
        / ".omx"
        / "framework"
        / "team"
        / "visible-runs"
        / normalized["run_id"]
        / "tasks"
        / task_id
    )
    submitted = task_dir / ".submitted"
    if not task_dir.is_dir() or not submitted.is_file():
        return None
    if task_dir.is_symlink() or submitted.is_symlink():
        raise SupervisionDispatchError("worker receipt marker must not be a symlink")
    try:
        task_metadata = json.loads(
            (task_dir / "task.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisionDispatchError("worker receipt task metadata is invalid") from exc
    if not isinstance(task_metadata, dict):
        raise SupervisionDispatchError("worker receipt task metadata is invalid")

    worker_id = normalized["target"]["worker_id"]
    expected_contract = _worker_task_contract(normalized)
    if (
        str(task_metadata.get("id") or "") != task_id
        or str(task_metadata.get("run") or "") != normalized["run_id"]
        or str(task_metadata.get("owner") or "") != worker_id
        or task_metadata.get("task_contract") != expected_contract
    ):
        raise SupervisionDispatchError("worker receipt task does not match handoff")
    try:
        owner = (task_dir / "owner").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SupervisionDispatchError("worker receipt owner is unavailable") from exc
    if owner != worker_id:
        raise SupervisionDispatchError("worker receipt task owner does not match handoff")
    return {
        "schema_version": 1,
        "receipt_id": normalized["logical_action_id"],
        "run_id": normalized["run_id"],
        "task_id": task_id,
        "worker_id": worker_id,
        "receipt_state": "submitted",
        "task_contract_digest": _digest(expected_contract),
    }


def _worker_task_contract(normalized: dict[str, Any]) -> dict[str, Any]:
    contract = {
        "schema_version": 1,
        "supervision": {
            "logical_action_id": normalized["logical_action_id"],
            "workflow_revision": normalized["workflow_revision"],
            "edge_id": normalized["edge_id"],
            "effect_class": normalized["effect_class"],
            "evidence_snapshot_digest": normalized["evidence_snapshot_digest"],
            "source_attempts": normalized["source_attempts"],
            "forbidden_effects": [
                "merge",
                "install",
                "rebind",
                "session_rotate",
                "shared_write",
            ],
        },
    }
    if normalized["effect_class"] == "write_isolated":
        contract["isolated_write"] = normalized["target"]["isolated_write"]
    return contract


def preflight_readonly_worker(
    root: Path | str,
    worker_id: str,
    *,
    dispatch_path: Path | str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Read one worker's status, doctor, and lock truth without side effects."""
    root_path = Path(root).resolve()
    worker_id = _safe_id(worker_id, "worker_id")
    command_path = Path(dispatch_path)
    if runner is None:
        if not command_path.is_file():
            raise SupervisionDispatchError(
                f"visible dispatch command is unavailable: {command_path}"
            )
        runner = _subprocess_runner(root_path)
    command_prefix = [str(command_path)]

    status = _run_json(
        runner,
        command_prefix
        + ["status", "--root", str(root_path), "--worker", worker_id, "--json"],
        "worker status preflight",
    )
    workers = status.get("dispatchable_visible_workers")
    worker = next(
        (
            item
            for item in workers
            if isinstance(item, dict) and str(item.get("worker_id") or "") == worker_id
        ),
        None,
    ) if isinstance(workers, list) else None
    if not isinstance(worker, dict):
        raise SupervisionDispatchError("worker is not dispatchable")

    doctor = _run_json(
        runner,
        command_prefix
        + ["doctor", "--root", str(root_path), "--worker", worker_id, "--json"],
        "worker doctor preflight",
    )
    if doctor.get("ok") is not True:
        raise SupervisionDispatchError("worker doctor preflight is not healthy")

    locks = _run_json(
        runner,
        command_prefix
        + ["locks", "--root", str(root_path), "--worker", worker_id, "--json"],
        "worker lock preflight",
    )
    if locks.get("target_worker_available_for_dispatch") is not True:
        raise SupervisionDispatchError("target worker is not available for dispatch")

    return {
        "instance_id": _safe_id(worker.get("instance_id"), "binding instance_id"),
        "binding_generation": _positive_int(
            worker.get("binding_generation"),
            "binding_generation",
        ),
        "fencing_generation": _positive_int(
            worker.get("fencing_generation")
            or worker.get("binding_generation"),
            "fencing_generation",
        ),
    }


def _normalize_handoff(
    handoff: object,
    *,
    expected_effect_class: str,
) -> dict[str, Any]:
    if not isinstance(handoff, dict):
        raise SupervisionDispatchError("handoff must be an object")
    run_id = _safe_id(handoff.get("run_id"), "run_id")
    workflow_revision = _safe_id(
        handoff.get("workflow_revision"),
        "workflow_revision",
    )
    logical_action_id = _digest_value(
        handoff.get("logical_action_id"),
        "logical_action_id",
    )
    evidence_snapshot_digest = _digest_value(
        handoff.get("evidence_snapshot_digest"),
        "evidence_snapshot_digest",
    )
    edge_id = _safe_id(handoff.get("edge_id"), "edge_id")
    if handoff.get("effect_class") != expected_effect_class:
        raise SupervisionDispatchError(
            f"handoff effect_class must be {expected_effect_class}"
        )
    source_attempts = handoff.get("source_attempts")
    if not isinstance(source_attempts, list) or not source_attempts:
        raise SupervisionDispatchError("handoff source_attempts are missing")
    normalized_sources: list[dict[str, str]] = []
    for source in source_attempts:
        if not isinstance(source, dict):
            raise SupervisionDispatchError("handoff source attempt is invalid")
        normalized_sources.append(
            {
                "node_id": _safe_id(source.get("node_id"), "source node_id"),
                "attempt_id": _safe_id(source.get("attempt_id"), "source attempt_id"),
                "task_contract_digest": _digest_value(
                    source.get("task_contract_digest"),
                    "source task_contract_digest",
                ),
            }
        )
    raw_supervision = handoff.get("supervision")
    if not isinstance(raw_supervision, dict):
        raise SupervisionDispatchError("handoff supervision claim is missing")
    supervision = {
        "run_id": _safe_id(raw_supervision.get("run_id"), "supervision run_id"),
        "lease_epoch": _positive_int(
            raw_supervision.get("lease_epoch"),
            "supervision lease_epoch",
        ),
        "fencing_token": str(raw_supervision.get("fencing_token") or ""),
    }
    if supervision["fencing_token"] != (
        f"{supervision['run_id']}:{supervision['lease_epoch']}"
    ):
        raise SupervisionDispatchError("handoff supervision fencing_token is invalid")

    raw_target = handoff.get("target")
    if not isinstance(raw_target, dict):
        raise SupervisionDispatchError("handoff target is missing")
    task_title = " ".join(str(raw_target.get("task_title") or "").split())
    task_body = str(raw_target.get("task_body") or "")
    if not task_title or not task_body:
        raise SupervisionDispatchError("handoff target task template is missing")
    if raw_target.get("session_context_requirement") != "fresh_idle":
        raise SupervisionDispatchError(
            "handoff target must require fresh_idle session context"
        )
    raw_binding = raw_target.get("expected_binding")
    if not isinstance(raw_binding, dict):
        raise SupervisionDispatchError("handoff target binding is missing")
    binding = {
        "instance_id": _safe_id(raw_binding.get("instance_id"), "binding instance_id"),
        "binding_generation": _positive_int(
            raw_binding.get("binding_generation"),
            "binding_generation",
        ),
        "fencing_generation": _positive_int(
            raw_binding.get("fencing_generation"),
            "fencing_generation",
        ),
    }
    target = {
        "node_id": _safe_id(raw_target.get("node_id"), "target node_id"),
        "worker_id": _safe_id(raw_target.get("worker_id"), "target worker_id"),
        "task_title": task_title,
        "task_body": task_body,
        "session_context_requirement": "fresh_idle",
        "expected_binding": binding,
    }
    if expected_effect_class == "write_isolated":
        try:
            target["isolated_write"] = normalize_isolated_write_contract(
                raw_target.get("isolated_write")
            )
        except SupervisionPolicyError as exc:
            raise SupervisionDispatchError(str(exc)) from exc
    return {
        "run_id": run_id,
        "workflow_revision": workflow_revision,
        "logical_action_id": logical_action_id,
        "evidence_snapshot_digest": evidence_snapshot_digest,
        "edge_id": edge_id,
        "effect_class": expected_effect_class,
        "source_attempts": normalized_sources,
        "supervision": supervision,
        "target": target,
    }


def _verify_isolated_write_worktree(target: dict[str, Any]) -> None:
    worktree = Path(target["worktree_path"])
    shared_checkout = Path(target["shared_checkout_path"])
    if (
        not worktree.is_dir()
        or worktree.is_symlink()
        or not shared_checkout.is_dir()
        or shared_checkout.is_symlink()
    ):
        raise SupervisionDispatchError(
            "isolated_write worktree and shared checkout must be real directories"
        )
    worktree = worktree.resolve()
    shared_checkout = shared_checkout.resolve()
    if _paths_overlap(worktree, shared_checkout):
        raise SupervisionDispatchError(
            "isolated_write worktree overlaps the shared checkout"
        )
    head = _git_output(worktree, "rev-parse", "HEAD")
    if head != target["base_revision"]:
        raise SupervisionDispatchError("isolated_write base revision drift")
    if _git_output(worktree, "status", "--porcelain"):
        raise SupervisionDispatchError("isolated_write worktree must be clean")


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _git_output(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except OSError as exc:
        raise SupervisionDispatchError(
            "isolated_write git verification is unavailable"
        ) from exc
    if result.returncode != 0:
        raise SupervisionDispatchError("isolated_write git verification failed")
    return result.stdout.strip()


def _subprocess_runner(root: Path) -> Runner:
    def run(argv: list[str]) -> dict[str, Any]:
        result = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    return run


def _run_json(runner: Runner, argv: list[str], label: str) -> dict[str, Any]:
    result = _run_checked(runner, argv, label)
    try:
        payload = json.loads(str(result.get("stdout") or ""))
    except json.JSONDecodeError as exc:
        raise SupervisionDispatchError(f"{label} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise SupervisionDispatchError(f"{label} did not return an object")
    return payload


def _run_checked(runner: Runner, argv: list[str], label: str) -> dict[str, Any]:
    try:
        result = runner(argv)
    except Exception as exc:
        raise SupervisionDispatchError(f"{label} failed to start") from exc
    if not isinstance(result, dict):
        raise SupervisionDispatchError(f"{label} returned an invalid result")
    if int(result.get("returncode") or 0) != 0:
        detail = str(result.get("stderr") or result.get("stdout") or "").strip()
        raise SupervisionDispatchError(f"{label} failed: {detail or 'unknown error'}")
    return result


def _task_id(logical_action_id: str) -> str:
    return f"supervisor-{logical_action_id.removeprefix('sha256:')[:24]}"


def _safe_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise SupervisionDispatchError(f"{field} is invalid")
    return text


def _digest_value(value: object, field: str) -> str:
    text = str(value or "")
    if not SHA256.fullmatch(text):
        raise SupervisionDispatchError(f"{field} is invalid")
    return text


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise SupervisionDispatchError(f"{field} is invalid")
    return value


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
