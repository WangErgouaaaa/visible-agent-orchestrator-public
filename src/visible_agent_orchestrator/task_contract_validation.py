"""Optional postflight validation for exact visible-task handoff contracts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


def validate_task_contract(contract: Mapping[str, Any] | None, report_text: str) -> dict[str, Any]:
    payload = dict(contract or {})
    required_headings = _strings(payload.get("required_report_headings"))
    required_report_lines = _strings(payload.get("required_report_lines"))
    required_changed_files = _strings(payload.get("required_changed_files"))
    allowed_changed_files = _strings(payload.get("allowed_changed_files"))
    target_repo = str(payload.get("contract_target_repo") or "").strip()
    configured = bool(
        required_headings
        or required_report_lines
        or required_changed_files
        or allowed_changed_files
        or target_repo
    )
    if not configured:
        return {
            "configured": False,
            "semantic_status": "",
            "semantic_reason": "",
            "retry_scope": "",
            "errors": [],
        }

    headings, sections = _report_sections(report_text)
    errors = [
        f"missing_required_report_heading:{heading}"
        for heading in required_headings
        if _heading_key(heading) not in headings
    ]
    report_lines = str(report_text or "").splitlines()
    for required_line in required_report_lines:
        count = sum(line.strip() == required_line for line in report_lines)
        if count != 1:
            errors.append(
                f"required_report_line_count:{required_line}:{count}"
            )
    reported_files = _section_paths(sections.get("files", ""))
    reported_commit = _section_commit(sections.get("commit", ""))
    actual_changed_files: list[str] = []

    needs_git = bool(required_changed_files or allowed_changed_files)
    if needs_git and not target_repo:
        errors.append("contract_target_repo_required_for_changed_files")
    elif needs_git and not reported_commit:
        errors.append("reported_commit_missing")
    elif needs_git:
        actual_changed_files, git_error = _commit_files(Path(target_repo), reported_commit)
        if git_error:
            errors.append(git_error)
        else:
            missing = sorted(set(required_changed_files) - set(actual_changed_files))
            unexpected = sorted(set(actual_changed_files) - set(allowed_changed_files)) if allowed_changed_files else []
            if missing:
                errors.extend(f"required_changed_file_missing:{path}" for path in missing)
            if unexpected:
                errors.extend(f"changed_file_not_allowed:{path}" for path in unexpected)
            if reported_files and set(reported_files) != set(actual_changed_files):
                errors.append("reported_files_do_not_match_commit")

    return {
        "configured": True,
        "semantic_status": "failed" if errors else "passed",
        "semantic_reason": "report_contract_failed" if errors else "report_contract_passed",
        "retry_scope": "lane_only" if errors else "",
        "required_report_headings": required_headings,
        "required_report_lines": required_report_lines,
        "observed_report_headings": sorted(headings),
        "reported_files": reported_files,
        "reported_commit": reported_commit,
        "actual_changed_files": actual_changed_files,
        "required_changed_files": required_changed_files,
        "allowed_changed_files": allowed_changed_files,
        "contract_target_repo": target_repo,
        "errors": errors,
    }


def _report_sections(report_text: str) -> tuple[set[str], dict[str, str]]:
    headings: set[str] = set()
    sections: dict[str, list[str]] = {}
    current = ""
    for line in str(report_text or "").splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            current = _heading_key(match.group(1))
            headings.add(current)
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    return headings, {key: "\n".join(value) for key, value in sections.items()}


def _heading_key(value: str) -> str:
    return str(value or "").strip().rstrip(":").casefold()


def _section_paths(section: str) -> list[str]:
    paths: list[str] = []
    for line in str(section or "").splitlines():
        value = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", line).strip().strip("`")
        if value and "/" in value and not value.startswith(("http://", "https://")):
            paths.append(value)
    return sorted(dict.fromkeys(paths))


def _section_commit(section: str) -> str:
    match = re.search(r"\b[0-9a-fA-F]{7,64}\b", str(section or ""))
    return match.group(0) if match else ""


def _commit_files(repo: Path, commit: str) -> tuple[list[str], str]:
    if not repo.is_dir():
        return [], "contract_target_repo_not_found"
    try:
        resolved = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{commit}^{{commit}}"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        if resolved.returncode != 0:
            return [], "reported_commit_not_found"
        shown = subprocess.run(
            ["git", "-C", str(repo), "show", "--format=", "--name-only", resolved.stdout.strip()],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return [], "git_commit_diff_timeout"
    if shown.returncode != 0:
        return [], "git_commit_diff_failed"
    return sorted(dict.fromkeys(line.strip() for line in shown.stdout.splitlines() if line.strip())), ""


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
