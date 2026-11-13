from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_WIDTH = 40
DEFAULT_WIDTH = 72
ADVISORY_FOOTER = "advisory only: completion truth remains report.md + exactly one terminal marker"
STATUS_BADGES = {
    "created": "[created]",
    "running": "[running]",
    "waiting_input": "[waiting_input]",
    "blocked": "[blocked]",
    "failed": "[failed]",
    "completed": "[completed]",
    "unknown": "[unknown]",
}
DISPLAY_VALUE_KEYS = (
    "merged",
    "unique_value_realized",
    "confidence_only",
    "duplicate_or_overlap",
    "cancelled_before_merge",
    "retrieved_but_not_merged",
    "not_retrieved",
    "direct_path_converged_first",
)
DISPLAY_EFFICIENCY_KEYS = (
    "launched",
    "retrieved",
    "merged",
    "duplicate_terminal_rows",
    "terminal",
    "retrieved_but_not_merged",
    "not_merged",
    "direct_path_converged_first",
)


@dataclass(frozen=True)
class CompactProjectionOptions:
    title: str
    width: int | None = None
    label_subagent_fields: bool = True
    include_metadata: bool = True
    row_layout: bool = False
    warning_prefix: str = "  ! "
    empty_ledger_present_message: str = "(ledger present but empty)"


def render_compact_projection(projection: dict[str, Any], options: CompactProjectionOptions) -> str:
    width = _normalize_width(options.width)
    lines = [
        options.title,
        _maybe_fit(summary_line(projection), width),
    ]

    tasks = projection.get("tasks", []) if isinstance(projection.get("tasks"), list) else []
    if not tasks:
        lines.extend(["", "No OpenCode subagent ledger evidence found."])
    for task in tasks:
        lines.append("")
        if options.row_layout:
            lines.extend(_task_row_lines(task, options, width))
        else:
            lines.append(_maybe_fit(task_line(task), width))
            subagents = task.get("subagents", []) if isinstance(task.get("subagents"), list) else []
            if not subagents:
                message = empty_task_message(task, options)
                lines.append(f"  {message}")
            for subagent in subagents:
                lines.append(
                    _maybe_fit(
                        subagent_line(
                            subagent,
                            label_fields=options.label_subagent_fields,
                            include_metadata=options.include_metadata,
                        ),
                        width,
                    )
                )
        for warning in task.get("warnings", []):
            lines.append(_maybe_fit(f"{options.warning_prefix}{warning}", width))
    lines.append("")
    lines.extend(wrap_footer(width))
    return "\n".join(lines)


def format_value_summary(projection: dict[str, Any]) -> str:
    summary = projection.get("value_summary") if isinstance(projection.get("value_summary"), dict) else {}
    chunks = []
    for key in DISPLAY_VALUE_KEYS:
        count = int(summary.get(key, 0) or 0)
        if count:
            chunks.append(f"{key}={count}")
    return ", ".join(chunks)


def summary_line(projection: dict[str, Any]) -> str:
    summary = projection.get("summary", {}) if isinstance(projection.get("summary"), dict) else {}
    status_summary = projection.get("status_summary", {}) if isinstance(projection.get("status_summary"), dict) else {}
    task_count = int(summary.get("task_count", 0) or 0)
    ledger_count = int(summary.get("ledger_present_tasks", 0) or 0)
    subagent_count = int(summary.get("subagent_count", 0) or 0)
    warning_count = int(summary.get("warning_count", 0) or 0)
    status_text = ", ".join(f"{key}={value}" for key, value in status_summary.items()) or "-"
    line = (
        f"source={projection.get('source') or 'none'} tasks={task_count} "
        f"ledgers={ledger_count}/{task_count} subagents={subagent_count} "
        f"warnings={warning_count} statuses={status_text}"
    )
    value_text = value_summary_text(projection)
    if value_text:
        line = f"{line} values={value_text}"
    efficiency_text = efficiency_summary_text(projection)
    return f"{line} efficiency={efficiency_text}" if efficiency_text else line


def value_summary_text(projection: dict[str, Any]) -> str:
    value_summary = projection.get("value_summary") if isinstance(projection.get("value_summary"), dict) else {}
    chunks = []
    for key in DISPLAY_VALUE_KEYS:
        count = int(value_summary.get(key, 0) or 0)
        if count:
            chunks.append(f"{key}={count}")
    return ", ".join(chunks)


def efficiency_summary_text(projection: dict[str, Any]) -> str:
    efficiency_summary = (
        projection.get("efficiency_summary") if isinstance(projection.get("efficiency_summary"), dict) else {}
    )
    chunks = []
    for key in DISPLAY_EFFICIENCY_KEYS:
        count = int(efficiency_summary.get(key, 0) or 0)
        if count:
            chunks.append(f"{key}={count}")
    return ", ".join(chunks)


def task_line(task: dict[str, Any]) -> str:
    return (
        f"{task.get('run') or '-'} {task.get('task_id') or '-'} "
        f"worker={task.get('parent_worker_id') or '-'} "
        f"ledger={task.get('ledger_status') or '-'}"
    )


def empty_task_message(task: dict[str, Any], options: CompactProjectionOptions) -> str:
    return (
        options.empty_ledger_present_message
        if task.get("ledger_status") == "present"
        else "(no subagent ledger yet)"
    )


def subagent_line(
    subagent: dict[str, Any],
    *,
    label_fields: bool,
    include_metadata: bool,
) -> str:
    artifact = "yes" if artifact_available(subagent) else "no"
    if label_fields:
        body = (
            f"  {status_badge(subagent.get('status'))} {subagent.get('subagent_id') or '-'} "
            f"agent={subagent.get('agent') or '-'} "
            f"lane={subagent.get('lane') or '-'} "
            f"artifact={artifact}"
        )
    else:
        body = (
            f"  {status_badge(subagent.get('status'))} {subagent.get('subagent_id') or '-'} "
            f"{subagent.get('agent') or '-'} {subagent.get('lane') or '-'} artifact={artifact}"
        )
    if include_metadata:
        body += compact_metadata(
            "retrieval",
            subagent.get("retrieval_state"),
            "merge",
            subagent.get("merge_readiness"),
        )
    return body


def row_task_anchor(task: dict[str, Any]) -> str:
    run_id = str(task.get("run") or "-")
    task_id = str(task.get("task_id") or "-")
    return f"{run_id}:{task_id}"


def task_empty_row(task: dict[str, Any], options: CompactProjectionOptions) -> str:
    if options.label_subagent_fields:
        return (
            f"[task] worker={task.get('parent_worker_id') or '-'} "
            f"task={row_task_anchor(task)} ledger={task.get('ledger_status') or '-'} "
            f"{empty_task_message(task, options)}"
        )
    return (
        f"[task] {task.get('parent_worker_id') or '-'} {row_task_anchor(task)} "
        f"ledger={task.get('ledger_status') or '-'} {empty_task_message(task, options)}"
    )


def subagent_row_line(
    task: dict[str, Any],
    subagent: dict[str, Any],
    *,
    label_fields: bool,
    include_metadata: bool,
) -> str:
    artifact = "yes" if artifact_available(subagent) else "no"
    if label_fields:
        body = (
            f"{status_badge(subagent.get('status'))} "
            f"worker={task.get('parent_worker_id') or '-'} "
            f"task={row_task_anchor(task)} "
            f"sg={subagent.get('subagent_id') or '-'} "
            f"agent={subagent.get('agent') or '-'} "
            f"lane={subagent.get('lane') or '-'} "
            f"artifact={artifact}"
        )
    else:
        body = (
            f"{status_badge(subagent.get('status'))} "
            f"{task.get('parent_worker_id') or '-'} {row_task_anchor(task)} "
            f"{subagent.get('subagent_id') or '-'} "
            f"{subagent.get('agent') or '-'} {subagent.get('lane') or '-'} "
            f"artifact={artifact}"
        )
    if include_metadata:
        body += compact_metadata(
            "retrieval",
            subagent.get("retrieval_state"),
            "merge",
            subagent.get("merge_readiness"),
        )
    return body


def status_badge(status: Any) -> str:
    value = str(status or "-")
    return STATUS_BADGES.get(value, f"[{value}]")


def artifact_available(subagent: dict[str, Any]) -> bool:
    if "artifact_present" in subagent:
        return bool(subagent.get("artifact_present"))
    return bool(subagent.get("artifact_exists"))


def compact_metadata(*pairs: Any) -> str:
    chunks: list[str] = []
    for idx in range(0, len(pairs), 2):
        key = str(pairs[idx])
        value = str(pairs[idx + 1] or "") if idx + 1 < len(pairs) else ""
        if value:
            chunks.append(f"{key}={value}")
    return "" if not chunks else " " + " ".join(chunks)


def fit(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def wrap_footer(width: int | None = None) -> list[str]:
    if width is None or len(ADVISORY_FOOTER) <= width:
        return [ADVISORY_FOOTER]
    prefix = "advisory only: completion truth remains report.md"
    suffix = "+ exactly one terminal marker"
    return [fit(prefix, width), fit(suffix, width)]


def _normalize_width(width: int | None) -> int | None:
    if width is None:
        return None
    return max(MIN_WIDTH, int(width or DEFAULT_WIDTH))


def _maybe_fit(value: str, width: int | None) -> str:
    return value if width is None else fit(value, width)


def _task_row_lines(
    task: dict[str, Any],
    options: CompactProjectionOptions,
    width: int | None,
) -> list[str]:
    subagents = task.get("subagents", []) if isinstance(task.get("subagents"), list) else []
    if not subagents:
        return [_maybe_fit(task_empty_row(task, options), width)]
    return [
        _maybe_fit(
            subagent_row_line(
                task,
                subagent,
                label_fields=options.label_subagent_fields,
                include_metadata=options.include_metadata,
            ),
            width,
        )
        for subagent in subagents
    ]
