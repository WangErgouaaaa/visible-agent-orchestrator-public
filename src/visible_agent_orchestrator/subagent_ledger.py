from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from visible_agent_orchestrator.subagent_projection import (
    VALID_BACKGROUND_EXPIRY_STATUSES,
    VALID_HEPHAESTUS_DRAFT_ADOPTIONS,
    VALID_MERGE_READINESS,
    VALID_MERGE_STATUSES,
    VALID_MOMUS_CONTEXT_ISOLATION_STATUSES,
    VALID_PARENT_ACTIONS_ON_DEGRADATION,
    VALID_RETRIEVAL_STATES,
    VALID_SOURCE_LABELS,
    VALID_SUBAGENT_STATUSES,
    VALID_VALUE_CLASSIFICATIONS,
)


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
LEDGER_JSONL = "subagent_ledger.jsonl"
STARTED_STATUSES = {"created", "launched", "running", "waiting_input"}
COMPLETED_STATUSES = {"blocked", "failed", "completed"}
REVIEWER_SEQUENCE_FAILURE_SEMANTICS = {
    "blocked",
    "failed",
    "empty_response",
    "parse_error",
    "provider_error",
    "provider_interruption",
    "schema_invalid",
    "timeout",
    "transport_error",
}
LEGACY_MERGE_READINESS_ALIASES = {
    "retrieved_but_not_merged": "not_merged",
}


class SubagentLedgerError(Exception):
    """Raised when task-local subagent ledger input is unsafe or malformed."""


def append_subagent_ledger_entry(
    root: Path | str,
    task_dir: Path | str,
    *,
    worker_id: str,
    subagent_id: str,
    agent: str,
    lane: str,
    purpose: str,
    status: str,
    artifact: str = "",
    artifact_path: str = "",
    artifact_summary: dict[str, Any] | str = "",
    timestamp: str = "",
    semantic_status: str = "",
    retry_scope: str = "",
    required_or_optional: str = "",
    latest_useful_moment: str = "",
    retrieval_state: str = "",
    merge_readiness: str = "",
    subdomain_id: str = "",
    spawned_by_worker: str = "",
    expected_unique_value: str = "",
    unique_value_realized: str = "",
    value_classification: str = "",
    merge_status: str = "",
    downstream_consumer: str = "",
    on_critical_path: bool | str = False,
    direct_path_converged_first: bool | str = False,
    cancel_reason: str = "",
    visible_role: str = "",
    evidence_domain: str = "",
    allowed_sources: list[str] | None = None,
    forbidden_sources: list[str] | None = None,
    source_label: str = "",
    adopted_findings: list[str] | None = None,
    rejected_findings: list[str] | None = None,
    critical_rejected_findings: list[str] | None = None,
    rejected_finding_reason: str = "",
    rejected_finding_direct_evidence_status: str = "",
    rejected_finding_downstream_action: str = "",
    subagent_degraded: bool | str = False,
    degradation_reason: str = "",
    confidence_effect: str = "",
    parent_action_on_degradation: str = "",
    librarian_freshness_source: str = "",
    librarian_freshness_checked_at: str = "",
    librarian_freshness_basis: str = "",
    hephaestus_draft_adoption: str = "",
    hephaestus_adoption_diff_or_summary: str = "",
    momus_context_isolation_status: str = "",
    background_expiry_status: str = "",
    late_result_handling: str = "",
    canonical_retrieval_handle: str = "",
    fallback_session_id: str = "",
    requested_launcher_contract: str = "",
    actual_launcher_used: str = "",
    missing_or_unexpressed_fields: list[str] | None = None,
    mitigation_steps_taken: list[str] | None = None,
    audit_status: str = "",
    review_round: int = 1,
    attempt: int = 1,
    context_manifest_sha256: str = "",
    trace_id: str = "",
    prompt_tokens: int | str | None = None,
    completion_tokens: int | str | None = None,
    total_tokens: int | str | None = None,
    token_cost_source: str = "",
    duration_ms: int | str | None = None,
    duration_ms_source: str = "",
    reviewer_sequence: str = "",
    write_task_local: bool = False,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    task_scope = resolve_visible_task_scope(
        root_path,
        Path(task_dir).resolve(strict=False),
    )
    run = str(task_scope["run"])
    task = str(task_scope["task"])
    task_path = Path(task_scope["ledger_dir"])
    worker = _required("worker", worker_id)
    normalized_agent = _required("agent", agent)
    normalized_status = _required("status", status)
    if normalized_status not in VALID_SUBAGENT_STATUSES:
        raise SubagentLedgerError(_unknown_status_message(normalized_status))
    normalized_review_round = _positive_int("review_round", review_round)
    scoped_review_round = task_scope.get("review_round")
    if (
        task_scope.get("nested")
        and normalized_review_round != scoped_review_round
    ):
        raise SubagentLedgerError("review_round_mismatch")
    normalized_attempt = _positive_int("attempt", attempt)
    normalized_trace_id = str(trace_id or "").strip()
    normalized_context_manifest_sha256 = str(context_manifest_sha256 or "").strip()
    normalized_reviewer_sequence = _normalize_reviewer_sequence(reviewer_sequence)
    if normalized_reviewer_sequence and not write_task_local:
        raise SubagentLedgerError("reviewer_sequence_requires_write_task_local")
    if (
        normalized_context_manifest_sha256
        and HEX64_RE.fullmatch(normalized_context_manifest_sha256) is None
    ):
        raise SubagentLedgerError("bad_context_manifest_sha256")
    normalized_value_classification = str(value_classification or "").strip()
    if normalized_value_classification and normalized_value_classification not in VALID_VALUE_CLASSIFICATIONS:
        raise SubagentLedgerError(f"unknown_value_classification:{normalized_value_classification}")
    normalized_merge_status = str(merge_status or "").strip()
    if normalized_merge_status and normalized_merge_status not in VALID_MERGE_STATUSES:
        raise SubagentLedgerError(f"unknown_merge_status:{normalized_merge_status}")
    normalized_merge_readiness = _normalize_merge_readiness(merge_readiness)
    if normalized_merge_readiness and normalized_merge_readiness not in VALID_MERGE_READINESS:
        raise SubagentLedgerError(_unknown_merge_readiness_message(normalized_merge_readiness))
    normalized_source_label = str(source_label or "").strip()
    if normalized_source_label and normalized_source_label not in VALID_SOURCE_LABELS:
        raise SubagentLedgerError("unknown_source_label:" + normalized_source_label)
    normalized_parent_action = str(parent_action_on_degradation or "").strip()
    if normalized_parent_action and normalized_parent_action not in VALID_PARENT_ACTIONS_ON_DEGRADATION:
        raise SubagentLedgerError("unknown_parent_action_on_degradation:" + normalized_parent_action)
    normalized_hephaestus_adoption = str(hephaestus_draft_adoption or "").strip()
    if normalized_hephaestus_adoption and normalized_hephaestus_adoption not in VALID_HEPHAESTUS_DRAFT_ADOPTIONS:
        raise SubagentLedgerError("unknown_hephaestus_draft_adoption:" + normalized_hephaestus_adoption)
    normalized_momus_isolation = str(momus_context_isolation_status or "").strip()
    if normalized_momus_isolation and normalized_momus_isolation not in VALID_MOMUS_CONTEXT_ISOLATION_STATUSES:
        raise SubagentLedgerError("unknown_momus_context_isolation_status:" + normalized_momus_isolation)
    normalized_background_expiry = str(background_expiry_status or "").strip()
    if normalized_background_expiry and normalized_background_expiry not in VALID_BACKGROUND_EXPIRY_STATUSES:
        raise SubagentLedgerError("unknown_background_expiry_status:" + normalized_background_expiry)
    normalized_hephaestus_diff = str(hephaestus_adoption_diff_or_summary or "").strip()
    normalized_prompt_tokens = _optional_nonnegative_int("prompt_tokens", prompt_tokens)
    normalized_completion_tokens = _optional_nonnegative_int("completion_tokens", completion_tokens)
    normalized_total_tokens = _optional_nonnegative_int("total_tokens", total_tokens)
    normalized_duration_ms = _optional_nonnegative_int("duration_ms", duration_ms)
    if (
        normalized_total_tokens is None
        and normalized_prompt_tokens is not None
        and normalized_completion_tokens is not None
    ):
        normalized_total_tokens = normalized_prompt_tokens + normalized_completion_tokens
    if (
        normalized_total_tokens is not None
        and normalized_prompt_tokens is not None
        and normalized_completion_tokens is not None
        and normalized_total_tokens != normalized_prompt_tokens + normalized_completion_tokens
    ):
        raise SubagentLedgerError("total_tokens_mismatch_prompt_plus_completion")
    if normalized_agent == "hephaestus" and normalized_status == "completed":
        hephaestus_blockers: list[str] = []
        if not normalized_hephaestus_adoption:
            hephaestus_blockers.append("completed_hephaestus_row_requires_draft_adoption")
        if not normalized_hephaestus_diff:
            hephaestus_blockers.append("completed_hephaestus_row_requires_adoption_diff_or_summary")
        if hephaestus_blockers:
            raise SubagentLedgerError(" | ".join(hephaestus_blockers))

    entry: dict[str, Any] = {
        "schema_version": 1,
        "parent_worker_id": worker,
        "parent_run_id": f"visible-runs/{run}",
        "parent_task_id": task,
        "subagent_id": _required("subagent_id", subagent_id),
        "agent": normalized_agent,
        "lane": _required("lane", lane),
        "purpose": _required("purpose", purpose),
        "status": normalized_status,
    }
    if timestamp and normalized_status in STARTED_STATUSES:
        entry["started_at"] = timestamp
    if timestamp and normalized_status in COMPLETED_STATUSES:
        entry["completed_at"] = timestamp
    for key, value in (
        ("artifact", artifact),
        ("artifact_path", artifact_path),
        ("semantic_status", semantic_status),
        ("retry_scope", retry_scope),
        ("required_or_optional", required_or_optional),
        ("latest_useful_moment", latest_useful_moment),
        ("retrieval_state", retrieval_state),
        ("merge_readiness", normalized_merge_readiness),
        ("subdomain_id", subdomain_id),
        ("spawned_by_worker", spawned_by_worker),
        ("expected_unique_value", expected_unique_value),
        ("unique_value_realized", unique_value_realized),
        ("value_classification", normalized_value_classification),
        ("merge_status", normalized_merge_status),
        ("downstream_consumer", downstream_consumer),
        ("cancel_reason", cancel_reason),
        ("visible_role", visible_role),
        ("evidence_domain", evidence_domain),
        ("source_label", normalized_source_label),
        ("rejected_finding_reason", rejected_finding_reason),
        ("rejected_finding_direct_evidence_status", rejected_finding_direct_evidence_status),
        ("rejected_finding_downstream_action", rejected_finding_downstream_action),
        ("degradation_reason", degradation_reason),
        ("confidence_effect", confidence_effect),
        ("parent_action_on_degradation", normalized_parent_action),
        ("librarian_freshness_source", librarian_freshness_source),
        ("librarian_freshness_checked_at", librarian_freshness_checked_at),
        ("librarian_freshness_basis", librarian_freshness_basis),
        ("hephaestus_draft_adoption", normalized_hephaestus_adoption),
        ("hephaestus_adoption_diff_or_summary", normalized_hephaestus_diff),
        ("momus_context_isolation_status", normalized_momus_isolation),
        ("background_expiry_status", normalized_background_expiry),
        ("late_result_handling", late_result_handling),
        ("canonical_retrieval_handle", canonical_retrieval_handle),
        ("fallback_session_id", fallback_session_id),
        ("requested_launcher_contract", requested_launcher_contract),
        ("actual_launcher_used", actual_launcher_used),
        ("audit_status", audit_status),
    ):
        normalized = str(value or "").strip()
        if normalized:
            entry[key] = normalized
    if isinstance(artifact_summary, dict):
        entry["artifact_summary"] = dict(artifact_summary)
    elif str(artifact_summary or "").strip():
        entry["artifact_summary"] = str(artifact_summary).strip()
    for key, value in (
        ("allowed_sources", allowed_sources),
        ("forbidden_sources", forbidden_sources),
        ("adopted_findings", adopted_findings),
        ("rejected_findings", rejected_findings),
        ("critical_rejected_findings", critical_rejected_findings),
        ("missing_or_unexpressed_fields", missing_or_unexpressed_fields),
        ("mitigation_steps_taken", mitigation_steps_taken),
    ):
        normalized_list = _string_list(value)
        if normalized_list:
            entry[key] = normalized_list
    for key, value in (
        ("on_critical_path", on_critical_path),
        ("direct_path_converged_first", direct_path_converged_first),
        ("subagent_degraded", subagent_degraded),
    ):
        if _has_bool_value(value):
            entry[key] = _bool(value)
    for key, value in (
        ("prompt_tokens", normalized_prompt_tokens),
        ("completion_tokens", normalized_completion_tokens),
        ("total_tokens", normalized_total_tokens),
        ("duration_ms", normalized_duration_ms),
    ):
        if value is not None:
            entry[key] = value
    for key, value in (
        ("token_cost_source", token_cost_source),
        ("duration_ms_source", duration_ms_source),
    ):
        normalized = str(value or "").strip()
        if normalized:
            entry[key] = normalized

    ledger_path = task_path / LEDGER_JSONL
    if normalized_reviewer_sequence and normalized_status == "launched":
        enforce_reviewer_sequence_gate(
            ledger_path,
            agent=normalized_agent,
            reviewer_sequence=normalized_reviewer_sequence,
        )
    write_skipped_duplicate = False
    if write_task_local:
        write_skipped_duplicate = not append_jsonl(ledger_path, entry)
    return {
        "schema_version": 1,
        "helper_kind": "subagent_ledger",
        "writes_task_local": bool(write_task_local),
        "write_skipped_duplicate": write_skipped_duplicate,
        "control_writes": False,
        "ledger_path": rel(ledger_path, root_path),
        "entry": entry,
        "advisory": "subagent_ledger.jsonl is advisory evidence only and does not change completion truth",
    }


def parse_visible_task_path(root: Path, task_dir: Path) -> tuple[str, str]:
    resolved = task_dir.resolve(strict=False)
    try:
        parts = resolved.relative_to(visible_runs_root(root)).parts
    except ValueError as exc:
        raise SubagentLedgerError("unsafe_task_dir") from exc
    if len(parts) != 3 or parts[1] != "tasks" or parts[0] in {"_locks", "_watcher"}:
        raise SubagentLedgerError("unsafe_task_dir")
    return parts[0], parts[2]


def resolve_visible_task_scope(root: Path, task_dir: Path) -> dict[str, Any]:
    resolved = task_dir.resolve(strict=False)
    try:
        parts = resolved.relative_to(visible_runs_root(root)).parts
    except ValueError as exc:
        raise SubagentLedgerError("unsafe_task_dir") from exc
    if (
        len(parts) == 3
        and parts[1] == "tasks"
        and parts[0] not in {"_locks", "_watcher"}
    ):
        return {
            "run": parts[0],
            "task": parts[2],
            "review_round": 1,
            "parent_task_dir": resolved,
            "ledger_dir": resolved,
            "nested": False,
        }
    if (
        len(parts) == 5
        and parts[1] == "tasks"
        and parts[0] not in {"_locks", "_watcher"}
        and parts[3] == "review_rounds"
        and re.fullmatch(r"round-[1-9][0-9]*", parts[4])
    ):
        parent = visible_runs_root(root) / parts[0] / "tasks" / parts[2]
        return {
            "run": parts[0],
            "task": parts[2],
            "review_round": int(parts[4].split("-", 1)[1]),
            "parent_task_dir": parent,
            "ledger_dir": resolved,
            "nested": True,
        }
    raise SubagentLedgerError("unsafe_task_dir")


@contextmanager
def jsonl_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SubagentLedgerError("unsafe_jsonl_lock") from exc
    with os.fdopen(descriptor, "r+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            opened = os.fstat(handle.fileno())
            try:
                current = os.stat(lock_path, follow_symlinks=False)
            except OSError as exc:
                raise SubagentLedgerError("unsafe_jsonl_lock") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise SubagentLedgerError("unsafe_jsonl_lock")
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_jsonl(path: Path, entry: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    with jsonl_lock(path):
        try:
            descriptor = os.open(path, flags, 0o666)
        except OSError as exc:
            raise SubagentLedgerError("unsafe_jsonl_path") from exc
        with os.fdopen(descriptor, "r+b") as handle:
            opened = os.fstat(handle.fileno())
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise SubagentLedgerError("unsafe_jsonl_path") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise SubagentLedgerError("unsafe_jsonl_path")
            handle.seek(0)
            content = handle.read()
            duplicate = False
            lines = content.splitlines(keepends=True)
            offset = 0
            for index, raw_line in enumerate(lines):
                line = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
                if not line.strip():
                    offset += len(raw_line)
                    continue
                try:
                    existing = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    if (
                        index == len(lines) - 1
                        and encoded.startswith(raw_line)
                    ):
                        handle.seek(offset)
                        handle.truncate()
                        if not duplicate:
                            handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                        return not duplicate
                    reason = (
                        "invalid_jsonl_tail"
                        if index == len(lines) - 1
                        else "invalid_jsonl"
                    )
                    raise SubagentLedgerError(reason) from exc
                if not isinstance(existing, dict):
                    reason = (
                        "invalid_jsonl_tail"
                        if index == len(lines) - 1
                        else "invalid_jsonl"
                    )
                    raise SubagentLedgerError(reason)
                if existing == entry:
                    duplicate = True
                offset += len(raw_line)
            if duplicate:
                return False
            handle.seek(0, os.SEEK_END)
            if content and not content.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            return True


def enforce_reviewer_sequence_gate(
    ledger_path: Path,
    *,
    agent: str,
    reviewer_sequence: tuple[str, ...],
) -> None:
    """Reject a reviewer launch when an earlier required role is not successful."""
    try:
        current_index = reviewer_sequence.index(agent)
    except ValueError as exc:
        raise SubagentLedgerError(
            f"reviewer_sequence_gate_unknown_role:role={agent}"
        ) from exc
    if current_index == 0:
        return

    rows: list[dict[str, Any]] = []
    if ledger_path.is_file():
        for line_number, line in enumerate(
            ledger_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SubagentLedgerError(
                    f"reviewer_sequence_gate_invalid_ledger:line={line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise SubagentLedgerError(
                    f"reviewer_sequence_gate_invalid_ledger:line={line_number}"
                )
            rows.append(row)

    for prior_role in reviewer_sequence[:current_index]:
        prior_rows: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if str(row.get("agent") or "").strip() != prior_role:
                continue
            subagent_id = str(row.get("subagent_id") or "").strip()
            if not subagent_id:
                raise SubagentLedgerError(
                    f"reviewer_sequence_gate_invalid_ledger:role={prior_role}"
                )
            prior_rows.setdefault(subagent_id, []).append(row)
        if not prior_rows:
            raise SubagentLedgerError(
                f"reviewer_sequence_gate_missing_prior_role:role={prior_role}"
            )
        for subagent_id, lifecycle_rows in prior_rows.items():
            latest = lifecycle_rows[-1]
            if _reviewer_sequence_row_succeeded(latest):
                continue
            raise SubagentLedgerError(
                "reviewer_sequence_gate_blocked:"
                f"role={prior_role}:subagent_id={subagent_id}:"
                f"{_reviewer_sequence_row_state(latest)}"
            )


def _reviewer_sequence_row_succeeded(row: dict[str, Any]) -> bool:
    if str(row.get("status") or "").strip().lower() != "completed":
        return False
    semantic_status = str(row.get("semantic_status") or "").strip().lower()
    if semantic_status in REVIEWER_SEQUENCE_FAILURE_SEMANTICS:
        return False
    if any(
        marker in semantic_status
        for marker in ("error", "fail", "interrupt", "timeout", "blocked")
    ):
        return False
    summary = row.get("artifact_summary")
    if isinstance(summary, dict) and str(summary.get("failure_class") or "").strip():
        return False
    return True


def _reviewer_sequence_row_state(row: dict[str, Any]) -> str:
    state = f"status={str(row.get('status') or '').strip() or 'missing'}"
    semantic_status = str(row.get("semantic_status") or "").strip()
    if semantic_status:
        state += f":semantic_status={semantic_status}"
    summary = row.get("artifact_summary")
    if isinstance(summary, dict):
        failure_class = str(summary.get("failure_class") or "").strip()
        if failure_class:
            state += f":failure_class={failure_class}"
    return state


def _normalize_reviewer_sequence(value: str) -> tuple[str, ...]:
    raw = str(value or "").strip()
    if not raw:
        return ()
    roles = tuple(part.strip() for part in raw.split(","))
    if any(not role for role in roles):
        raise SubagentLedgerError("reviewer_sequence_contains_empty_role")
    if len(set(roles)) != len(roles):
        raise SubagentLedgerError("reviewer_sequence_contains_duplicate_role")
    return roles


def visible_runs_root(root: Path) -> Path:
    return root.resolve() / ".omx" / "framework" / "team" / "visible-runs"


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _required(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SubagentLedgerError(f"missing_{name}")
    return normalized


def _has_bool_value(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return bool(str(value or "").strip())


def _bool(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _string_list(value: list[str] | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_nonnegative_int(name: str, value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SubagentLedgerError(f"{name}_must_be_an_integer") from exc
    if normalized < 0:
        raise SubagentLedgerError(f"{name}_must_be_nonnegative")
    return normalized


def _positive_int(name: str, value: int | str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SubagentLedgerError(f"bad_{name}") from exc
    if normalized <= 0:
        raise SubagentLedgerError(f"bad_{name}")
    return normalized


def _normalize_merge_readiness(value: str) -> str:
    normalized = str(value or "").strip()
    return LEGACY_MERGE_READINESS_ALIASES.get(normalized, normalized)


def _unknown_merge_readiness_message(value: str) -> str:
    return "unknown_merge_readiness:" + value + "; valid_merge_readiness:" + ",".join(sorted(VALID_MERGE_READINESS))


def _unknown_status_message(value: str) -> str:
    parts = [f"unknown_status:{value}"]
    if value in VALID_RETRIEVAL_STATES and value:
        parts.append(f"did_you_mean:--retrieval-state {value}")
    parts.append("valid_statuses:" + ",".join(sorted(VALID_SUBAGENT_STATUSES)))
    return "; ".join(parts)
