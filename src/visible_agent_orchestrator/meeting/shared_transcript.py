"""Shared transcript artifacts for leader-mediated visible worker review."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerReportReference:
    round_index: int
    worker_id: str
    role: str
    run_id: str
    task_id: str
    report_path: Path


def build_shared_worker_transcript(
    *,
    task: str,
    report_refs: list[WorkerReportReference],
    max_report_chars: int = 1200,
) -> dict[str, object]:
    if not task.strip():
        raise ValueError("shared worker transcript task must be non-empty")
    if not report_refs:
        raise ValueError("shared worker transcript requires at least one report reference")
    if max_report_chars < 80:
        raise ValueError("max_report_chars must be at least 80")

    sanitized_task = _sanitize_report_text(task.strip())
    reports = [_build_report_entry(ref, max_report_chars=max_report_chars) for ref in report_refs]
    return {
        "schema_version": 1,
        "artifact_kind": "leader_mediated_worker_shared_transcript",
        "scope": "leader_mediated_worker_group_review",
        "task": sanitized_task,
        "would_execute_live": False,
        "dispatches_visible_workers": False,
        "reads_host_local_config": False,
        "worker_count": len({entry["worker_id"] for entry in reports}),
        "round_count": len({entry["round_index"] for entry in reports}),
        "reports": reports,
        "next_round_prompt_context": _render_next_round_prompt_context(sanitized_task, reports),
        "safety_notes": [
            "advisory transcript only; leader remains the dispatch authority",
            "host-local paths and provider tokens are redacted before reuse in worker prompts",
            "no report markers, worker locks, raw tmux input, /tui/* calls, or visible dispatch are performed",
        ],
    }


def write_shared_worker_transcript_artifacts(
    root: Path,
    thread_id: str,
    transcript: dict[str, object],
) -> dict[str, Path]:
    if not thread_id.strip():
        raise ValueError("shared worker transcript thread_id must be non-empty")
    target = root / thread_id
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "shared-transcript.json"
    md_path = target / "shared-transcript.md"
    json_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(_render_shared_transcript_markdown(transcript), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def build_leader_mediated_worker_group_review(
    *,
    task: str,
    report_refs: list[WorkerReportReference],
    round_limit: int,
    max_report_chars: int = 1200,
) -> dict[str, object]:
    if round_limit < 1:
        raise ValueError("round_limit must be at least 1")

    transcript = build_shared_worker_transcript(
        task=task,
        report_refs=report_refs,
        max_report_chars=max_report_chars,
    )
    reports = list(transcript["reports"])
    current_round = max(int(report["round_index"]) for report in reports)
    latest_reports = [report for report in reports if report["round_index"] == current_round]
    decision = _decide_next_worker_group_round(latest_reports=latest_reports, current_round=current_round, round_limit=round_limit)
    sanitized_task = str(transcript["task"])
    return {
        "schema_version": 1,
        "artifact_kind": "leader_mediated_worker_group_review",
        "scope": "leader_mediated_worker_group_review",
        "task": sanitized_task,
        "would_execute_live": False,
        "dispatches_visible_workers": False,
        "reads_host_local_config": False,
        "round_limit": round_limit,
        "latest_round": {
            "round_index": current_round,
            "worker_ids": [str(report["worker_id"]) for report in latest_reports],
            "roles": [str(report["role"]) for report in latest_reports],
            "verdicts": [str(report["verdict"]) for report in latest_reports],
            "terminal_states": [str(report["terminal_state"]) for report in latest_reports],
        },
        "decision": decision,
        "shared_transcript": transcript,
        "next_round_prompt": _render_worker_group_next_round_prompt(
            task=sanitized_task,
            transcript=transcript,
            decision=decision,
        ),
        "safety_notes": [
            "advisory leader artifact only; this command does not dispatch visible workers",
            "the Codex leader owns speaker selection, prompt construction, and final synthesis",
            "workers must not receive host-local AG2 runtime paths, provider endpoints, model names, or secret-bearing config",
        ],
    }


def write_leader_mediated_worker_group_review_artifacts(
    root: Path,
    thread_id: str,
    review: dict[str, object],
) -> dict[str, Path]:
    if not thread_id.strip():
        raise ValueError("worker group review thread_id must be non-empty")
    target = root / thread_id
    target.mkdir(parents=True, exist_ok=True)
    transcript_paths = write_shared_worker_transcript_artifacts(
        root,
        thread_id,
        review["shared_transcript"],
    )
    review_json = target / "worker-group-review.json"
    review_md = target / "worker-group-review.md"
    next_round_prompt = target / "next-round-prompt.md"
    review_json.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    review_md.write_text(_render_worker_group_review_markdown(review), encoding="utf-8")
    next_round_prompt.write_text(str(review["next_round_prompt"]) + "\n", encoding="utf-8")
    return {
        "review_json": review_json,
        "review_md": review_md,
        "next_round_prompt_md": next_round_prompt,
        "shared_transcript_json": transcript_paths["json"],
        "shared_transcript_md": transcript_paths["markdown"],
    }


def _build_report_entry(ref: WorkerReportReference, *, max_report_chars: int) -> dict[str, object]:
    report_text = ref.report_path.read_text(encoding="utf-8")
    sanitized = _sanitize_report_text(report_text)
    excerpt = sanitized[:max_report_chars].rstrip()
    truncated = len(sanitized) > len(excerpt)
    return {
        "round_index": ref.round_index,
        "worker_id": ref.worker_id,
        "role": ref.role,
        "run_id": ref.run_id,
        "task_id": ref.task_id,
        "report_ref": f"{ref.run_id}/{ref.task_id}/{ref.report_path.name}",
        "report_bytes": len(report_text.encode("utf-8")),
        "terminal_state": _terminal_state_for_report(ref.report_path),
        "verdict": _extract_verdict(sanitized),
        "summary": _extract_summary(sanitized),
        "excerpt": excerpt,
        "truncated": truncated,
        "redactions": _redaction_summary(report_text, sanitized),
    }


def _terminal_state_for_report(report_path: Path) -> str:
    task_dir = report_path.parent
    states = [state for state in ("done", "fail", "blocked") if (task_dir / f".{state}").exists()]
    if len(states) == 1:
        return states[0]
    if len(states) > 1:
        return "conflicting_terminal_markers"
    return "unknown"


def _extract_verdict(text: str) -> str:
    match = re.search(r"(?im)^(?:final\s+verdict|verdict)\s*:\s*([A-Z][A-Z_-]*)", text)
    return match.group(1) if match else "UNKNOWN"


def _extract_summary(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "summary:":
            collected: list[str] = []
            for candidate in lines[index + 1 :]:
                stripped = candidate.strip()
                if not stripped:
                    if collected:
                        break
                    continue
                collected.append(stripped)
                if len(" ".join(collected)) >= 320:
                    break
            if collected:
                return " ".join(collected)[:400]
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped[:400]
    return ""


def _sanitize_report_text(text: str) -> str:
    text = re.sub(
        (
            r"(?i)\b(?:api_key|base_url|openai_api_key|deepseek_api_key|model|"
            r"provider_endpoint|endpoint|api_base|api_url|authorization|bearer|token)\b"
            r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"
        ),
        "<redacted-provider-field>",
        text,
    )
    text = re.sub(r"/tmp/[^\s,;:)]+", "<redacted-host-local-path>", text)
    text = re.sub(r"/(?:home|srv|opt)/[^/\s]+/\.config/[^\s,;:)]+", "<redacted-host-local-path>", text)
    text = re.sub(
        (
            r"(?i)\b(api_key|base_url|openai_api_key|deepseek_api_key|model|"
            r"provider_endpoint|endpoint|api_base|api_url|authorization|bearer|token|v4-pro)\b"
        ),
        "<redacted-provider-token>",
        text,
    )
    return text


def _redaction_summary(original: str, sanitized: str) -> dict[str, object]:
    return {
        "applied": original != sanitized,
        "host_local_path_redacted": "<redacted-host-local-path>" in sanitized,
        "provider_field_redacted": "<redacted-provider-field>" in sanitized,
        "provider_token_redacted": (
            "<redacted-provider-token>" in sanitized or "<redacted-provider-field>" in sanitized
        ),
    }


def _render_next_round_prompt_context(task: str, reports: list[dict[str, object]]) -> str:
    lines = [
        f"Shared transcript for task: {task}",
        "",
        "Previous worker reports:",
    ]
    for report in reports:
        lines.append(
            "- "
            f"Round {report['round_index']} {report['worker_id']} "
            f"({report['role']}): verdict={report['verdict']}; summary={report['summary']}"
        )
    lines.extend(
        [
            "",
            "Use this transcript as advisory context only. Do not read host-local approval/runtime JSON, "
            "do not dispatch other workers, and do not use raw tmux or /tui/*.",
        ]
    )
    return "\n".join(lines)


def _decide_next_worker_group_round(
    *,
    latest_reports: list[dict[str, object]],
    current_round: int,
    round_limit: int,
) -> dict[str, object]:
    verdicts = [str(report["verdict"]) for report in latest_reports]
    terminal_states = [str(report["terminal_state"]) for report in latest_reports]
    if any(state in {"fail", "blocked", "conflicting_terminal_markers"} for state in terminal_states):
        return {
            "state": "stop",
            "reason": "latest-round terminal blocker detected",
            "next_round_index": None,
            "stop_condition_met": True,
        }
    if any(verdict in {"FAIL", "FAILED", "BLOCKED"} for verdict in verdicts):
        return {
            "state": "stop",
            "reason": "latest-round blocking verdict detected",
            "next_round_index": None,
            "stop_condition_met": True,
        }
    if any(state != "done" for state in terminal_states):
        if current_round >= round_limit:
            return {
                "state": "stop",
                "reason": "round limit reached before all latest reports reached done",
                "next_round_index": None,
                "stop_condition_met": True,
            }
        return {
            "state": "continue",
            "reason": "latest-round reports are missing done terminal markers",
            "next_round_index": current_round + 1,
            "stop_condition_met": False,
        }
    if verdicts and all(verdict == "PASS" for verdict in verdicts):
        return {
            "state": "stop",
            "reason": "all latest-round worker reports passed",
            "next_round_index": None,
            "stop_condition_met": True,
        }
    if current_round >= round_limit:
        return {
            "state": "stop",
            "reason": "round limit reached",
            "next_round_index": None,
            "stop_condition_met": True,
        }
    return {
        "state": "continue",
        "reason": "latest-round reports need another leader-mediated review round",
        "next_round_index": current_round + 1,
        "stop_condition_met": False,
    }


def _render_worker_group_next_round_prompt(
    *,
    task: str,
    transcript: dict[str, object],
    decision: dict[str, object],
) -> str:
    if decision["state"] != "continue":
        return (
            f"Task: {task}\n\n"
            f"No next worker round is required. Stop reason: {decision['reason']}.\n"
            "The Codex leader should synthesize the final answer from the shared transcript."
        )
    return "\n".join(
        [
            f"Task: {task}",
            f"Next leader-mediated worker round: {decision['next_round_index']}",
            "",
            "Use the shared transcript below as advisory context. Address unresolved PARTIAL/UNKNOWN findings first.",
            "Do not dispatch other workers. Do not read host-local AG2 approval/runtime JSON. Do not use raw tmux or /tui/*.",
            "",
            str(transcript["next_round_prompt_context"]),
        ]
    )


def _render_worker_group_review_markdown(review: dict[str, object]) -> str:
    decision = review["decision"]
    lines = [
        "# Leader-Mediated Worker Group Review",
        "",
        f"- task: {review['task']}",
        f"- artifact_kind: {review['artifact_kind']}",
        f"- would_execute_live: {str(review['would_execute_live']).lower()}",
        f"- dispatches_visible_workers: {str(review['dispatches_visible_workers']).lower()}",
        f"- round_limit: {review['round_limit']}",
        "",
        "## Decision",
        "",
        f"- state: {decision['state']}",
        f"- reason: {decision['reason']}",
        f"- next_round_index: {decision['next_round_index']}",
        "",
        "## Latest Round",
        "",
    ]
    latest_round = review["latest_round"]
    for worker_id, role, verdict, terminal_state in zip(
        latest_round["worker_ids"],
        latest_round["roles"],
        latest_round["verdicts"],
        latest_round["terminal_states"],
    ):
        lines.append(f"- {worker_id} ({role}): verdict={verdict}; terminal_state={terminal_state}")
    lines.extend(["", "## Next Round Prompt", "", str(review["next_round_prompt"])])
    return "\n".join(lines) + "\n"


def _render_shared_transcript_markdown(transcript: dict[str, object]) -> str:
    lines = [
        "# Shared Worker Transcript",
        "",
        f"- task: {transcript['task']}",
        f"- artifact_kind: {transcript['artifact_kind']}",
        f"- worker_count: {transcript['worker_count']}",
        f"- round_count: {transcript['round_count']}",
        f"- would_execute_live: {str(transcript['would_execute_live']).lower()}",
        f"- dispatches_visible_workers: {str(transcript['dispatches_visible_workers']).lower()}",
        "",
        "## Reports",
    ]
    for report in transcript["reports"]:
        lines.extend(
            [
                "",
                f"### Round {report['round_index']} - {report['worker_id']}",
                f"- role: {report['role']}",
                f"- verdict: {report['verdict']}",
                f"- terminal_state: {report['terminal_state']}",
                "",
                str(report["summary"]),
            ]
        )
    lines.extend(["", "## Next Round Prompt Context", "", str(transcript["next_round_prompt_context"])])
    return "\n".join(lines) + "\n"
