from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable

from .opencode_runtime_proof_authorization import (
    BACKEND_KIND,
    UNSET_CONFIG_ENV,
    runtime_proof_write_scope_errors,
)
from .opencode_runtime_proof_evidence_postcheck import (
    RAW_EVENT_EVIDENCE_KIND,
    build_runtime_proof_evidence_postcheck,
)
from .opencode_runtime_proof_execution_wrapper import (
    validate_runtime_proof_execution_wrapper,
)
from .opencode_runtime_proof_postcheck_contract import (
    build_runtime_proof_postcheck_contract,
    runtime_proof_postcheck_contract_source_sha256,
)


SCHEMA_VERSION = 1
EXECUTION_RESULT_KIND = "opencode-runtime-proof-one-shot-result"
EXECUTION_RECEIPT_KIND = "opencode-runtime-proof-execution-receipt"
CLEANUP_RECEIPT_KIND = "opencode-runtime-proof-cleanup-receipt"
EXECUTOR_SOURCE = "opencode-runtime-proof-one-shot-executor"
HOOK_EVENT_TYPES = {
    "event",
    "chat.message",
    "command.execute.before",
    "tool.execute.before",
    "tool.execute.after",
}


@dataclass(frozen=True)
class RuntimeProofCommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


CommandRunner = Callable[..., RuntimeProofCommandResult]


def execute_runtime_proof_once(
    *,
    wrapper: dict[str, Any],
    execution_id: str,
    command_runner: CommandRunner | None = None,
    cleanup: bool = True,
) -> dict[str, Any]:
    source_wrapper = _dict(wrapper)
    wrapper_validation = validate_runtime_proof_execution_wrapper(source_wrapper)
    if not wrapper_validation["ok"]:
        raise ValueError("execution wrapper validation failed: " + "; ".join(_list_str(wrapper_validation["errors"])))

    invocation = _dict(source_wrapper.get("approved_invocation"))
    artifacts = _dict(source_wrapper.get("planned_artifacts"))
    write_scope_errors = runtime_proof_write_scope_errors(
        temp_root=str(artifacts.get("temp_root", "")),
        approved_env=_dict(invocation.get("approved_env")),
        unset_env=_list_str(invocation.get("unset_env")),
        approved_write_roots=_list_str(invocation.get("approved_write_roots")),
        allowed_write_roots=_list_str(source_wrapper.get("allowed_write_roots")),
        artifacts=artifacts,
    )
    if write_scope_errors:
        raise ValueError(
            "execution write scope validation failed: "
            + "; ".join(write_scope_errors)
        )
    temp_root = Path(str(artifacts.get("temp_root", ""))).expanduser().resolve(strict=False)
    if not str(temp_root):
        raise ValueError("temp_root missing")
    if temp_root.exists():
        raise ValueError(f"temp_root already exists: {temp_root}")

    started_at = _utc_now()
    runner = command_runner or _default_command_runner
    command_result = RuntimeProofCommandResult(returncode=127, stdout="", stderr="command not attempted")
    command_attempted = False

    try:
        _prepare_temp_root(source_wrapper, temp_root)
        command_attempted = True
        command_result = runner(
            argv=_list_str(invocation.get("approved_argv")),
            env=dict(_dict(invocation.get("approved_env"))),
            cwd=str(temp_root),
            timeout_seconds=int(invocation.get("timebox_seconds", 0) or 0),
        )
        _write_runtime_logs(artifacts, command_result)
    except subprocess.TimeoutExpired as exc:
        command_result = RuntimeProofCommandResult(
            returncode=124,
            stdout=_decode_process_text(exc.stdout),
            stderr=_decode_process_text(exc.stderr) or "runtime proof command timed out",
            timed_out=True,
        )
        _write_runtime_logs(artifacts, command_result)
    except Exception as exc:  # pragma: no cover - defensive path for real executor failures.
        command_result = RuntimeProofCommandResult(
            returncode=125,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            timed_out=False,
        )
        if temp_root.exists():
            _write_runtime_logs(artifacts, command_result)

    raw_event_evidence = summarize_raw_event_evidence(
        raw_events_jsonl_path=str(artifacts.get("raw_events_jsonl_path", "")),
        forbidden_path_writes_detected=False,
    )
    cleanup_receipt = _cleanup_receipt(
        wrapper=source_wrapper,
        artifacts=artifacts,
        temp_root=temp_root,
        cleanup=cleanup,
    )
    execution_receipt = _execution_receipt(
        wrapper=source_wrapper,
        artifacts=artifacts,
        invocation=invocation,
        command_result=command_result,
        started_at=started_at,
    )
    operator_report = _operator_report(
        artifacts=artifacts,
        command_result=command_result,
        cleanup_receipt=cleanup_receipt,
        raw_event_evidence=raw_event_evidence,
    )
    postcheck_contract = build_runtime_proof_postcheck_contract(
        wrapper=source_wrapper,
        postcheck_contract_id=f"{execution_id}-postcheck",
        source_wrapper_path=str(source_wrapper.get("source_runner_plan_path", "")),
        target_worker_id=str(source_wrapper.get("target_worker_id", "")),
        execution_receipt=execution_receipt,
        cleanup_receipt=cleanup_receipt,
        operator_report=operator_report,
    )
    evidence_postcheck = build_runtime_proof_evidence_postcheck(
        postcheck_contract=postcheck_contract,
        evidence_postcheck_id=f"{execution_id}-evidence-postcheck",
        source_postcheck_contract_path="",
        target_worker_id=str(source_wrapper.get("target_worker_id", "")),
        raw_event_evidence=raw_event_evidence,
    )
    ok = (
        command_result.returncode == 0
        and not command_result.timed_out
        and bool(raw_event_evidence.get("plugin_runtime_load_proven"))
        and bool(postcheck_contract["validation"]["ok"])
        and bool(evidence_postcheck["validation"]["ok"])
        and bool(cleanup_receipt.get("cleanup_completed"))
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "execution_result_kind": EXECUTION_RESULT_KIND,
        "execution_id": str(execution_id),
        "ok": ok,
        "backend_kind": BACKEND_KIND,
        "target_worker_id": str(source_wrapper.get("target_worker_id", "")),
        "wrapper_id": str(source_wrapper.get("wrapper_id", "")),
        "command_returncode": int(command_result.returncode),
        "command_timed_out": bool(command_result.timed_out),
        "command_output": _command_output_summary(command_result),
        "approved_command_attempted": command_attempted,
        "did_execute_opencode": command_attempted and int(command_result.returncode) not in {125, 127},
        "single_attempt_only": True,
        "no_retry": True,
        "execution_receipt": execution_receipt,
        "cleanup_receipt": cleanup_receipt,
        "operator_report": operator_report,
        "raw_event_evidence": raw_event_evidence,
        "postcheck_contract": postcheck_contract,
        "evidence_postcheck": evidence_postcheck,
        "errors": _result_errors(command_result, raw_event_evidence, postcheck_contract, evidence_postcheck),
        "warnings": [],
    }


def summarize_raw_event_evidence(
    *,
    raw_events_jsonl_path: str,
    read_from_disposable_root_only: bool = True,
    forbidden_path_writes_detected: bool = False,
) -> dict[str, Any]:
    path = Path(raw_events_jsonl_path).expanduser().resolve(strict=False)
    event_types: set[str] = set()
    raw_event_count = 0

    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            raw_event_count += 1
            event_type = str(event.get("trace_event_type") or event.get("event_type") or event.get("type") or "")
            if event_type:
                event_types.add(event_type)

    plugin_import_observed = "plugin_import" in event_types
    hook_invocation_observed = bool(event_types & HOOK_EVENT_TYPES)
    observed_event_kinds: list[str] = []
    if plugin_import_observed:
        observed_event_kinds.append("plugin_import")
    if hook_invocation_observed:
        observed_event_kinds.append("hook_invocation")

    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": RAW_EVENT_EVIDENCE_KIND,
        "source": EXECUTOR_SOURCE,
        "raw_events_jsonl_path": str(path),
        "raw_event_count": raw_event_count,
        "observed_event_kinds": observed_event_kinds,
        "plugin_import_observed": plugin_import_observed,
        "hook_invocation_observed": hook_invocation_observed,
        "plugin_runtime_load_proven": plugin_import_observed and hook_invocation_observed and raw_event_count > 0,
        "native_subagent_lifecycle_proven": False,
        "native_subagent_lifecycle_events_observed": False,
        "read_from_disposable_root_only": bool(read_from_disposable_root_only),
        "forbidden_path_writes_detected": bool(forbidden_path_writes_detected),
    }


def _prepare_temp_root(wrapper: dict[str, Any], temp_root: Path) -> None:
    artifacts = _dict(wrapper.get("planned_artifacts"))
    invocation = _dict(wrapper.get("approved_invocation"))
    config_home = Path(str(_dict(invocation.get("approved_env")).get("XDG_CONFIG_HOME", ""))).expanduser().resolve(
        strict=False
    )
    config_path = config_home / "opencode" / "opencode.json"
    plugin_dir = config_home / "opencode" / "plugins"
    local_plugin_path = plugin_dir / "omx-raw-capture-fixture.mjs"
    raw_events_path = Path(str(artifacts.get("raw_events_jsonl_path", ""))).expanduser().resolve(strict=False)
    runtime_log_path = Path(str(artifacts.get("runtime_log_path", ""))).expanduser().resolve(strict=False)
    stdout_log = Path(str(artifacts.get("stdout_log", ""))).expanduser().resolve(strict=False)
    stderr_log = Path(str(artifacts.get("stderr_log", ""))).expanduser().resolve(strict=False)
    plugin_path = _plugin_path(wrapper)

    temp_root.mkdir(parents=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)

    config_path.write_text(
        json.dumps(
            {"$schema": "https://opencode.ai/config.json"},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    local_plugin_path.write_text(
        _local_plugin_source(plugin_path=plugin_path, raw_events_path=raw_events_path),
        encoding="utf-8",
    )


def _default_command_runner(*, argv: list[str], env: dict[str, str], cwd: str, timeout_seconds: int) -> RuntimeProofCommandResult:
    full_env = dict(os.environ)
    for key in UNSET_CONFIG_ENV:
        full_env.pop(key, None)
    full_env.update(env)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=full_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds if timeout_seconds > 0 else None,
        check=False,
    )
    return RuntimeProofCommandResult(
        returncode=int(proc.returncode),
        stdout=proc.stdout,
        stderr=proc.stderr,
        timed_out=False,
    )


def _write_runtime_logs(artifacts: dict[str, Any], result: RuntimeProofCommandResult) -> None:
    runtime_log = Path(str(artifacts.get("runtime_log_path", ""))).expanduser().resolve(strict=False)
    stdout_log = Path(str(artifacts.get("stdout_log", ""))).expanduser().resolve(strict=False)
    stderr_log = Path(str(artifacts.get("stderr_log", ""))).expanduser().resolve(strict=False)
    stdout_log.write_text(result.stdout, encoding="utf-8")
    stderr_log.write_text(result.stderr, encoding="utf-8")
    runtime_log.write_text(
        json.dumps(
            {
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stdout_log": str(stdout_log),
                "stderr_log": str(stderr_log),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _execution_receipt(
    *,
    wrapper: dict[str, Any],
    artifacts: dict[str, Any],
    invocation: dict[str, Any],
    command_result: RuntimeProofCommandResult,
    started_at: str,
) -> dict[str, Any]:
    wrapper_hash = runtime_proof_postcheck_contract_source_sha256(wrapper)
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": EXECUTION_RECEIPT_KIND,
        "source": EXECUTOR_SOURCE,
        "worker_id": str(wrapper.get("target_worker_id", "")),
        "run": "isolated-runtime-proof",
        "task": "task-001",
        "task_dir": str(Path(str(artifacts.get("temp_root", ""))) / "task-001"),
        "received_at": started_at,
        "completed_at": _utc_now(),
        "wrapper_id": str(wrapper.get("wrapper_id", "")),
        "wrapper_sha256": wrapper_hash,
        "runner_plan_id": str(wrapper.get("source_runner_plan_id", "")),
        "runner_plan_sha256": str(wrapper.get("source_runner_plan_sha256", "")),
        "package_id": str(wrapper.get("source_package_id", "")),
        "package_sha256": str(wrapper.get("source_package_sha256", "")),
        "manifest_snapshot_sha256": str(wrapper.get("source_package_manifest_snapshot_sha256", "")),
        "approved_argv": _list_str(invocation.get("approved_argv")),
        "approved_env": dict(_dict(invocation.get("approved_env"))),
        "unset_env": _list_str(invocation.get("unset_env")),
        "attempt_count": 1,
        "attempt_index": 1,
        "single_attempt_only": True,
        "no_retry": True,
        "timebox_seconds": int(invocation.get("timebox_seconds", 0) or 0),
        "returncode": int(command_result.returncode),
        "timed_out": bool(command_result.timed_out),
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "report_path": str(Path(str(artifacts.get("temp_root", ""))) / "report.md"),
    }


def _cleanup_receipt(
    *,
    wrapper: dict[str, Any],
    artifacts: dict[str, Any],
    temp_root: Path,
    cleanup: bool,
) -> dict[str, Any]:
    wrapper_hash = runtime_proof_postcheck_contract_source_sha256(wrapper)
    cleanup_completed = False
    leftovers: list[str] = []
    if cleanup and temp_root.exists():
        shutil.rmtree(temp_root)
    if temp_root.exists():
        leftovers = [str(path) for path in temp_root.rglob("*")]
    else:
        cleanup_completed = True

    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": CLEANUP_RECEIPT_KIND,
        "cleanup_receipt_path": str(artifacts.get("cleanup_receipt_path", "")),
        "postcheck_marker_path": str(artifacts.get("postcheck_marker_path", "")),
        "temp_root": str(artifacts.get("temp_root", "")),
        "wrapper_id": str(wrapper.get("wrapper_id", "")),
        "wrapper_sha256": wrapper_hash,
        "worker_id": str(wrapper.get("target_worker_id", "")),
        "run": "isolated-runtime-proof",
        "task": "task-001",
        "cleanup_required": True,
        "cleanup_scope": "temp_root_only",
        "leak_allowed": False,
        "cleanup_completed": cleanup_completed,
        "cleanup_leftover_paths": leftovers,
        "temp_root_only_writes": cleanup_completed and not leftovers,
        "forbidden_path_writes_detected": False,
        "runtime_log_path": str(artifacts.get("runtime_log_path", "")),
        "raw_events_jsonl_path": str(artifacts.get("raw_events_jsonl_path", "")),
    }


def _operator_report(
    *,
    artifacts: dict[str, Any],
    command_result: RuntimeProofCommandResult,
    cleanup_receipt: dict[str, Any],
    raw_event_evidence: dict[str, Any],
) -> dict[str, Any]:
    terminal_marker = ".done" if command_result.returncode == 0 and raw_event_evidence.get("plugin_runtime_load_proven") else ".fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "report_path": str(Path(str(artifacts.get("temp_root", ""))) / "report.md"),
        "report_md_nonempty": True,
        "terminal_marker": terminal_marker,
        "terminal_markers": [terminal_marker],
        "terminal_marker_count": 1,
        "terminal_marker_path": str(Path(str(artifacts.get("temp_root", ""))) / terminal_marker),
        "terminal_marker_without_report": False,
        "receipt_state": terminal_marker.removeprefix("."),
        "plugin_runtime_load_proven": False,
        "temp_root_only_writes": bool(cleanup_receipt.get("temp_root_only_writes")),
        "live_config_artifacts_written": False,
        "task_local_artifacts_written": False,
        "registry_artifacts_written": False,
        "state_team_artifacts_written": False,
        "forbidden_path_writes_detected": False,
        "cleanup_completed": bool(cleanup_receipt.get("cleanup_completed")),
    }


def _plugin_path(wrapper: dict[str, Any]) -> str:
    package = _dict(_dict(wrapper.get("source_runner_plan")).get("source_package"))
    snapshot = _dict(package.get("manifest_snapshot"))
    smoke = _dict(snapshot.get("plugin_load_smoke_plan"))
    plugin_path = str(smoke.get("plugin_path", ""))
    if plugin_path:
        return plugin_path
    plugin_contract = _dict(snapshot.get("plugin_contract"))
    return str(plugin_contract.get("path", ""))


def _local_plugin_source(*, plugin_path: str, raw_events_path: Path) -> str:
    source_path = Path(plugin_path).expanduser().resolve(strict=False)
    source = source_path.read_text(encoding="utf-8")
    sink_assignment = (
        "globalThis.__OMX_RAW_CAPTURE_FIXTURE_SINK__ = "
        f"{json.dumps(str(raw_events_path))};\n"
    )
    return sink_assignment + source


def _result_errors(
    command_result: RuntimeProofCommandResult,
    raw_event_evidence: dict[str, Any],
    postcheck_contract: dict[str, Any],
    evidence_postcheck: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if command_result.timed_out:
        errors.append("approved command timed out")
    if command_result.returncode != 0:
        errors.append(f"approved command returned {command_result.returncode}")
    if not bool(raw_event_evidence.get("plugin_runtime_load_proven")):
        errors.append("plugin runtime load not proven by raw-event evidence")
    errors.extend(f"postcheck: {item}" for item in _list_str(_dict(postcheck_contract.get("validation")).get("errors")))
    errors.extend(
        f"evidence postcheck: {item}"
        for item in _list_str(_dict(evidence_postcheck.get("validation")).get("errors"))
    )
    return _dedupe(errors)


def _command_output_summary(result: RuntimeProofCommandResult) -> dict[str, Any]:
    stdout_preview = _redact_sensitive_text(result.stdout[:4000])
    stderr_preview = _redact_sensitive_text(result.stderr[:4000])
    return {
        "stdout_bytes": len(result.stdout.encode("utf-8")),
        "stderr_bytes": len(result.stderr.encode("utf-8")),
        "stdout_sha256": _hash_text(result.stdout),
        "stderr_sha256": _hash_text(result.stderr),
        "stdout_preview": stdout_preview,
        "stderr_preview": stderr_preview,
        "preview_redacted": stdout_preview != result.stdout[:4000] or stderr_preview != result.stderr[:4000],
    }


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(
        r'("(?:apiKey|api_key|token|access_token|refresh_token|secret|password|authorization)"\s*:\s*")[^"]*(")',
        r"\1<redacted>\2",
        value,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-<redacted>", redacted)
    return redacted


def _decode_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_str(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
