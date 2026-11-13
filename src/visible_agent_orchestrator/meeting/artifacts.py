"""Artifact writing for no-live meeting results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import MeetingResult, build_meeting_audit_summary, build_meeting_synthesis_summary


@dataclass(frozen=True)
class MeetingArtifactPaths:
    result_json: Path
    synthesis_json: Path
    audit_json: Path
    transcript_md: Path


def write_meeting_artifacts(
    root: Path,
    thread_id: str,
    result: MeetingResult,
    *,
    ag2_human_approval_validation: dict | None = None,
    ag2_host_local_config_reference_validation: dict | None = None,
) -> MeetingArtifactPaths:
    target = root / thread_id
    target.mkdir(parents=True, exist_ok=True)
    result_json = target / "meeting-result.json"
    synthesis_json = target / "meeting-synthesis.json"
    audit_json = target / "meeting-audit.json"
    transcript_md = target / "transcript.md"
    result_json.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    synthesis_json.write_text(
        json.dumps(build_meeting_synthesis_summary(result).to_payload(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    audit_json.write_text(
        json.dumps(
            build_meeting_audit_summary(
                ag2_human_approval_validation=ag2_human_approval_validation,
                ag2_host_local_config_reference_validation=ag2_host_local_config_reference_validation,
            ),
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    transcript_md.write_text(_render_transcript(result), encoding="utf-8")
    return MeetingArtifactPaths(
        result_json=result_json,
        synthesis_json=synthesis_json,
        audit_json=audit_json,
        transcript_md=transcript_md,
    )


def _render_transcript(result: MeetingResult) -> str:
    synthesis = build_meeting_synthesis_summary(result)
    lines = [
        f"# Meeting Transcript",
        "",
        f"- task: {result.task}",
        f"- termination_reason: {result.termination_reason}",
        f"- verdict: {result.verdict}",
        "",
        "## Synthesis",
        "",
        f"- method: {synthesis.method}",
        f"- verdict: {synthesis.verdict}",
        "",
        "### Risks",
        *_render_list(synthesis.risks),
        "",
        "### Open Questions",
        *_render_list(synthesis.open_questions),
        "",
        "### Recommended Next Actions",
        *_render_list(synthesis.recommended_next_actions),
        "",
        "## Messages",
    ]
    for message in result.messages:
        lines.extend(
            [
                "",
                f"### Round {message.round_index}: {message.speaker}",
                f"- role: {message.role}",
                "",
                message.content,
            ]
        )
    return "\n".join(lines) + "\n"


def _render_list(items: tuple[str, ...]) -> list[str]:
    if not items:
        return ["- (none)"]
    return [f"- {item}" for item in items]
