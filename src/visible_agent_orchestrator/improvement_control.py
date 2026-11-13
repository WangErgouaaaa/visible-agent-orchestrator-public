"""Pure, fail-closed primitives for evidence-governed improvement."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit

from .collaboration_profile import (
    FORBIDDEN_FIELDS,
    CollaborationProfileError,
    validate_collaboration_profile,
)


ISSUE_ARTIFACT_KIND = "improvement_issue_plan"
ISSUE_LEDGER_PREFIX = ("multi-agent_questions", "bug issues")
SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ISSUE_URL_RE = re.compile(r"^(https://github\.com/[^/]+/[^/]+)/issues/([1-9][0-9]*)$")
EPISODE_STATUSES = {"archival_only", "verified"}
EXPERIENCE_STATUSES = {
    "archival_only",
    "local_candidate",
    "approved_runtime_candidate",
    "approved_runtime",
    "needs_revalidation",
    "quarantined",
    "reinstated",
    "narrowed_scope",
    "superseded",
    "revoked",
}
EXPERIENCE_KINDS = {
    "success_pattern",
    "failure_prevention",
    "failure_lesson",
}
FAILURE_CLASSES = {
    "method_invalid",
    "execution_invalid",
    "environment_mismatch",
    "insufficient_evidence",
    "budget_exhausted",
    "unsafe",
    "regression_inducing",
    "not_applicable",
}
CANDIDATE_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_kind",
        "candidate_id",
        "level",
        "hypothesis",
        "target_failure_cluster",
        "change_surface",
        "experience_ids",
        "activation",
        "bounds",
        "profile",
        "rollback",
        "risk_class",
    }
)
CANDIDATE_ACTIVATION_FIELDS = frozenset(
    {
        "triggers",
        "prerequisites",
        "contraindications",
        "fallback",
    }
)
CANDIDATE_BOUNDS_FIELDS = frozenset(
    {
        "max_fanout",
        "max_retries",
        "token_budget",
    }
)
CANDIDATE_ROLLBACK_FIELDS = frozenset({"previous_version"})
CANDIDATE_SURFACE_BY_LEVEL = {
    "L0": "experience_scope",
    "L1": "retrieval_route",
    "L2": "skill_steps",
    "L3": "supervision_profile",
}
RELEASE_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_kind",
        "release_id",
        "candidate_id",
        "current_state",
        "requested_state",
        "automatic_promotion",
        "automatic_emergency_disable",
        "operator_approval",
        "evidence_refs",
        "rollback_version",
        "hard_invariant_failure",
    }
)
HARD_INVARIANT_FIELDS = frozenset(
    {
        "invariant_id",
        "evidence_ref",
        "evidence_sha256",
    }
)
HARD_INVARIANT_IDS = {
    "lease_cas",
    "owner_lock",
    "terminal_marker",
    "permission_boundary",
    "audit_integrity",
    "install_parity",
}
RELEASE_STATES = {
    "eligible_for_operator_review",
    "shadow",
    "canary",
    "active",
    "quarantined",
    "rolled_back",
}
REQUESTED_RELEASE_STATES = {
    "shadow",
    "canary",
    "active",
    "quarantined",
    "rolled_back",
}
FORWARD_RELEASE_EVIDENCE = {
    "shadow": (
        "eligible_for_operator_review",
        "evaluation:passed:",
        "evaluation_evidence_required",
    ),
    "canary": (
        "shadow",
        "shadow:passed:",
        "successful_shadow_evidence_required",
    ),
    "active": (
        "canary",
        "canary:passed:",
        "successful_canary_evidence_required",
    ),
}


class ImprovementControlError(ValueError):
    """Raised when an improvement-control boundary cannot safely proceed."""


def plan_issue_sync(
    *,
    issue_path: Path | str,
    repo_root: Path | str,
    remote_issues: Sequence[Mapping[str, Any]],
    source_commit: str,
    remote_url: str,
) -> dict[str, Any]:
    """Build a deterministic, non-executing GitHub Issue plan."""

    root = Path(repo_root).expanduser().resolve()
    path = Path(issue_path).expanduser().resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ImprovementControlError("issue path must be inside repository root") from exc
    if relative.parts[:2] != ISSUE_LEDGER_PREFIX:
        raise ImprovementControlError(
            "issue path must be inside the multi-agent bug issue ledger"
        )
    if not path.is_file():
        raise ImprovementControlError(f"issue source is not a file: {relative}")

    commit = str(source_commit).strip()
    if not SOURCE_COMMIT_RE.fullmatch(commit):
        raise ImprovementControlError("source_commit must be 40 lowercase hex")
    canonical_remote = _canonical_remote_url(remote_url)

    try:
        local_body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImprovementControlError(f"cannot read issue source: {relative}") from exc
    title = _frontmatter_title(local_body)
    source_path = relative.as_posix()
    source_marker = f"<!-- vao-ledger-source:{relative.name} -->"
    source_url = (
        f"{canonical_remote}/blob/{commit}/{quote(source_path, safe='/')}"
    )
    idempotency_key = hashlib.sha256(
        f"{source_marker}\0{commit}".encode("utf-8")
    ).hexdigest()
    body_parts = [f"Source: {source_url}", local_body.rstrip()]
    if source_marker not in local_body:
        body_parts.append(source_marker)
    body = "\n\n".join(body_parts).rstrip() + "\n"

    action, matched_issue, reason_codes = _remote_issue_decision(
        title=title,
        source_marker=source_marker,
        remote_issues=remote_issues,
    )
    return {
        "schema_version": 1,
        "artifact_kind": ISSUE_ARTIFACT_KIND,
        "action": action,
        "title": title,
        "source_path": source_path,
        "source_commit": commit,
        "source_url": source_url,
        "source_marker": source_marker,
        "idempotency_key": idempotency_key,
        "body": body,
        "labels": ["bug"],
        "matched_issue": matched_issue,
        "reason_codes": reason_codes,
    }


def derive_issue_claim_store_dir(
    plan: Mapping[str, Any],
    *,
    state_home: Path | str | None = None,
) -> Path:
    """Derive one host-local claim store for one canonical GitHub remote."""

    normalized = _validate_create_plan(plan)
    remote_url = _remote_url_from_source_url(
        normalized["source_url"],
        normalized["source_commit"],
    )
    configured_home = (
        Path(state_home).expanduser()
        if state_home is not None
        else (
            Path(os.environ["XDG_STATE_HOME"]).expanduser()
            if os.environ.get("XDG_STATE_HOME")
            else Path.home() / ".local" / "state"
        )
    )
    if not configured_home.is_absolute():
        raise ImprovementControlError(
            "XDG_STATE_HOME for issue claims must be an absolute path"
        )
    remote_hash = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()
    return (
        configured_home.resolve()
        / "visible-agent-orchestrator"
        / "improvement-issue-claims"
        / remote_hash
    )


def fetch_remote_issues_via_gh(
    plan: Mapping[str, Any],
    *,
    runner: Callable[..., Any],
) -> list[dict[str, Any]]:
    """Fetch every open and closed Issue page immediately before apply."""

    normalized = _validate_create_plan(plan)
    remote_url = _remote_url_from_source_url(
        normalized["source_url"],
        normalized["source_commit"],
    )
    repository = urlsplit(remote_url).path.strip("/")
    command = [
        "gh",
        "api",
        f"repos/{repository}/issues?state=all&per_page=100",
        "--paginate",
        "--slurp",
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            check=True,
            text=True,
        )
        pages = json.loads(str(getattr(completed, "stdout", "") or ""))
        if not isinstance(pages, list) or any(
            not isinstance(page, list)
            for page in pages
        ):
            raise ImprovementControlError(
                "GitHub paginated issue response must be a list of pages"
            )
        issues: list[dict[str, Any]] = []
        for page in pages:
            for raw_issue in page:
                if not isinstance(raw_issue, Mapping):
                    raise ImprovementControlError(
                        "GitHub issue page entries must be mappings"
                    )
                if "pull_request" in raw_issue:
                    continue
                number = raw_issue.get("number")
                if (
                    not isinstance(number, int)
                    or isinstance(number, bool)
                    or number <= 0
                ):
                    raise ImprovementControlError(
                        "GitHub issue number is invalid"
                    )
                state = str(raw_issue.get("state") or "").strip().lower()
                if state not in {"open", "closed"}:
                    raise ImprovementControlError(
                        "GitHub issue state is invalid"
                    )
                title = str(raw_issue.get("title") or "").strip()
                if not title:
                    raise ImprovementControlError(
                        "GitHub issue title is required"
                    )
                issue_url = str(raw_issue.get("html_url") or "").strip()
                if issue_url != f"{remote_url}/issues/{number}":
                    raise ImprovementControlError(
                        "GitHub issue URL does not match the planned remote"
                    )
                issues.append(
                    {
                        "number": number,
                        "title": title,
                        "body": str(raw_issue.get("body") or ""),
                        "state": state,
                        "html_url": issue_url,
                    }
                )
        return issues
    except ImprovementControlError:
        raise
    except Exception as exc:
        raise ImprovementControlError(
            "GitHub open and closed Issue recheck failed"
        ) from exc


def apply_issue_plan(
    *,
    plan: Mapping[str, Any],
    remote_issue_loader: Callable[[], Sequence[Mapping[str, Any]]],
    claim_store_dir: Path | str,
    runner: Callable[..., Any],
) -> dict[str, Any]:
    """Apply one create plan after remote revalidation and exclusive claim."""

    normalized = _validate_create_plan(plan)
    try:
        remote_issues = remote_issue_loader()
    except Exception as exc:
        raise ImprovementControlError(
            "remote recheck failed before issue claim acquisition"
        ) from exc
    action, matched_issue, _ = _remote_issue_decision(
        title=normalized["title"],
        source_marker=normalized["source_marker"],
        remote_issues=remote_issues,
    )
    if action != "create":
        number = matched_issue.get("number") if matched_issue else "unknown"
        raise ImprovementControlError(
            f"remote recheck blocked issue creation: {action} issue={number}"
        )

    claim_store = Path(claim_store_dir).expanduser().resolve()
    remote_url = _remote_url_from_source_url(
        normalized["source_url"],
        normalized["source_commit"],
    )
    _bind_claim_store(claim_store, remote_url)
    claims_dir = claim_store / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim_path = claims_dir / f"{normalized['idempotency_key']}.json"
    claim = {
        "schema_version": 1,
        "artifact_kind": "improvement_issue_claim",
        "idempotency_key": normalized["idempotency_key"],
        "remote_url": remote_url,
        "source_commit": normalized["source_commit"],
        "source_marker": normalized["source_marker"],
        "source_path": normalized["source_path"],
        "state": "claimed",
    }
    try:
        _write_exclusive_json(claim_path, claim)
    except FileExistsError as exc:
        raise ImprovementControlError(
            f"existing issue claim requires operator reconciliation: {claim_path}"
        ) from exc

    command = [
        "gh",
        "issue",
        "create",
        "--title",
        normalized["title"],
        "--body",
        normalized["body"],
        "--label",
        "bug",
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            check=True,
            text=True,
        )
        output = str(getattr(completed, "stdout", "") or "").strip()
        match = ISSUE_URL_RE.fullmatch(output)
        if match is None or match.group(1) != remote_url:
            raise ImprovementControlError(
                "gh issue create did not return the expected remote Issue URL"
            )
    except Exception as exc:
        raise ImprovementControlError(
            "Issue create outcome is uncertain; claim preserved for operator reconciliation"
        ) from exc

    return {
        "url": output,
        "number": int(match.group(2)),
        "claim_path": str(claim_path),
    }


def validate_episode(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an immutable Episode record."""

    record = _require_mapping(payload, "episode")
    if record.get("schema_version") != 1:
        raise ImprovementControlError("episode schema_version must be 1")
    if record.get("artifact_kind") != "improvement_episode":
        raise ImprovementControlError("episode artifact_kind is invalid")
    episode_id = _safe_id(record.get("episode_id"), "episode_id")
    status = _enum(record.get("status"), EPISODE_STATUSES, "episode status")

    issue = _require_mapping(record.get("issue"), "episode issue")
    source_path = _relative_reference(issue.get("source_path"), "issue source_path")
    snapshot_sha256 = _sha256(issue.get("snapshot_sha256"), "issue snapshot_sha256")
    github_url = str(issue.get("github_url") or "").strip()
    if github_url and ISSUE_URL_RE.fullmatch(github_url) is None:
        raise ImprovementControlError("episode issue github_url is invalid")
    issue_state = _enum(issue.get("state"), {"open", "closed"}, "issue state")

    evidence = _require_mapping(record.get("evidence"), "episode evidence")
    source_commit = str(evidence.get("source_commit") or "").strip()
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise ImprovementControlError("episode source_commit must be 40 lowercase hex")
    report = _evidence_reference(evidence.get("report"), "report")
    tests = _evidence_reference(evidence.get("tests"), "tests")
    live = _live_evidence(evidence.get("live"))

    environment_fingerprint = _json_mapping(
        record.get("environment_fingerprint"),
        "episode environment_fingerprint",
    )
    lineage = _episode_lineage(record.get("lineage"))
    return {
        "schema_version": 1,
        "artifact_kind": "improvement_episode",
        "episode_id": episode_id,
        "status": status,
        "issue": {
            "source_path": source_path,
            "snapshot_sha256": snapshot_sha256,
            "github_url": github_url,
            "state": issue_state,
        },
        "evidence": {
            "source_commit": source_commit,
            "report": report,
            "tests": tests,
            "live": live,
        },
        "environment_fingerprint": environment_fingerprint,
        "lineage": lineage,
    }


def validate_experience(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an Experience record."""

    record = _require_mapping(payload, "experience")
    if record.get("schema_version") != 1:
        raise ImprovementControlError("experience schema_version must be 1")
    if record.get("artifact_kind") != "improvement_experience":
        raise ImprovementControlError("experience artifact_kind is invalid")
    experience_id = _safe_id(record.get("id"), "experience id")
    version = record.get("version")
    if type(version) is not int or version <= 0:
        raise ImprovementControlError("experience version must be a positive integer")
    status = _enum(record.get("status"), EXPERIENCE_STATUSES, "experience status")
    kind = _enum(record.get("kind"), EXPERIENCE_KINDS, "experience kind")
    source_episode_id = _safe_id(
        record.get("source_episode_id"),
        "experience source_episode_id",
    )
    scope = _json_mapping(record.get("scope"), "experience scope")
    trigger = _string_list(record.get("trigger"), "experience trigger")
    prerequisites = _string_list(
        record.get("prerequisites"),
        "experience prerequisites",
    )
    contraindications = _string_list(
        record.get("contraindications"),
        "experience contraindications",
    )
    lesson = str(record.get("lesson") or "").strip()
    if not lesson:
        raise ImprovementControlError("experience lesson is required")
    failure_class = _enum(
        record.get("failure_class"),
        FAILURE_CLASSES,
        "experience failure_class",
    )
    evidence_refs = _experience_evidence_references(
        record.get("evidence_refs"),
    )
    environment_fingerprint = _json_mapping(
        record.get("environment_fingerprint"),
        "experience environment_fingerprint",
    )
    confidence = _enum(
        record.get("confidence"),
        {"low", "medium", "high"},
        "experience confidence",
    )
    lineage = _experience_lineage(record.get("lineage"))
    revalidate_on = _string_list(
        record.get("revalidate_on"),
        "experience revalidate_on",
    )
    runtime_hint = _runtime_hint(record.get("runtime_hint"))
    if status == "approved_runtime" and (
        not scope
        or not evidence_refs
        or not environment_fingerprint
        or any(not value for value in runtime_hint.values())
    ):
        raise ImprovementControlError(
            "approved_runtime requires scope, evidence, environment, and complete runtime_hint"
        )

    return {
        "schema_version": 1,
        "artifact_kind": "improvement_experience",
        "id": experience_id,
        "version": version,
        "status": status,
        "kind": kind,
        "source_episode_id": source_episode_id,
        "scope": scope,
        "trigger": trigger,
        "prerequisites": prerequisites,
        "contraindications": contraindications,
        "lesson": lesson,
        "failure_class": failure_class,
        "evidence_refs": evidence_refs,
        "environment_fingerprint": environment_fingerprint,
        "confidence": confidence,
        "lineage": lineage,
        "revalidate_on": revalidate_on,
        "runtime_hint": runtime_hint,
    }


def write_immutable_json(
    path_value: Path | str,
    payload: Mapping[str, Any],
) -> dict[str, str]:
    """Create one JSON record or report unchanged/conflict without overwrite."""

    path = Path(path_value).expanduser()
    normalized = _json_mapping(payload, "immutable JSON payload")
    desired_bytes = _json_bytes(normalized)
    desired_sha256 = f"sha256:{hashlib.sha256(desired_bytes).hexdigest()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_exclusive_json(path, normalized)
    except FileExistsError:
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
            observed_bytes = _json_bytes(observed)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return {
                "status": "conflict",
                "path": str(path),
                "sha256": desired_sha256,
            }
        status = "unchanged" if observed_bytes == desired_bytes else "conflict"
        return {
            "status": status,
            "path": str(path),
            "sha256": desired_sha256,
        }
    return {
        "status": "created",
        "path": str(path),
        "sha256": desired_sha256,
    }


def quarantine_experience(
    payload: Mapping[str, Any],
    *,
    reopened_issue_url: str,
    reopened_issue_sha256: str,
) -> dict[str, Any]:
    """Create a new quarantined version without mutating prior evidence."""

    normalized = validate_experience(payload)
    issue_url = str(reopened_issue_url or "").strip()
    if ISSUE_URL_RE.fullmatch(issue_url) is None:
        raise ImprovementControlError("reopened_issue_url must be a GitHub Issue URL")
    issue_sha256 = _sha256(
        reopened_issue_sha256,
        "reopened_issue_sha256",
    )
    result = deepcopy(normalized)
    result["version"] += 1
    result["status"] = "quarantined"
    reopened_reference = {
        "ref": issue_url,
        "sha256": issue_sha256,
    }
    if reopened_reference not in result["evidence_refs"]:
        result["evidence_refs"].append(reopened_reference)
    return validate_experience(result)


def select_runtime_memory(
    *,
    records: Sequence[Mapping[str, Any]],
    environment_fingerprint: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Select approved, current, applicable runtime hints without side effects."""

    if not isinstance(records, Sequence) or isinstance(
        records,
        (str, bytes, bytearray),
    ):
        raise ImprovementControlError("experience records must be a sequence")
    current_environment = _json_mapping(
        environment_fingerprint,
        "runtime environment_fingerprint",
    )
    runtime_context = _json_mapping(context, "runtime memory context")
    signals = set(
        _string_list(
            runtime_context.get("signals"),
            "runtime memory context signals",
        )
    )
    observed_scope = _json_mapping(
        runtime_context.get("scope"),
        "runtime memory context scope",
    )

    latest_by_id: dict[str, dict[str, Any]] = {}
    for payload in records:
        normalized = validate_experience(payload)
        experience_id = normalized["id"]
        existing = latest_by_id.get(experience_id)
        if existing is None or normalized["version"] > existing["version"]:
            latest_by_id[experience_id] = normalized
            continue
        if (
            normalized["version"] == existing["version"]
            and normalized != existing
        ):
            raise ImprovementControlError(
                f"conflicting duplicate experience version: "
                f"{experience_id}@{normalized['version']}"
            )

    latest = sorted(
        latest_by_id.values(),
        key=lambda item: (item["id"], item["version"]),
    )
    retrieved = [
        {
            "id": item["id"],
            "version": item["version"],
            "status": item["status"],
        }
        for item in latest
    ]
    needs_revalidation: list[dict[str, Any]] = []
    applicable: list[dict[str, Any]] = []
    for item in latest:
        if item["status"] != "approved_runtime":
            continue
        if item["environment_fingerprint"] != current_environment:
            needs_revalidation.append(
                {
                    "id": item["id"],
                    "version": item["version"],
                    "reason": "environment_fingerprint_mismatch",
                }
            )
            continue
        if not _scope_matches(item["scope"], observed_scope):
            continue
        if not set(item["trigger"]).issubset(signals):
            continue
        if not set(item["prerequisites"]).issubset(signals):
            continue
        if set(item["contraindications"]).intersection(signals):
            continue
        applicable.append(item)

    conflict_pairs: set[tuple[str, str]] = set()
    for index, left in enumerate(applicable):
        left_conflicts = set(left["lineage"]["conflicts_with"])
        for right in applicable[index + 1 :]:
            if (
                right["id"] in left_conflicts
                or left["id"] in set(right["lineage"]["conflicts_with"])
            ):
                conflict_pairs.add(tuple(sorted((left["id"], right["id"]))))
    blocked_ids = {item for pair in conflict_pairs for item in pair}

    selected = [item for item in applicable if item["id"] not in blocked_ids]
    selected.sort(
        key=lambda item: (
            -_scope_specificity(item["scope"]),
            -item["version"],
            item["id"],
        )
    )
    return {
        "retrieved": retrieved,
        "eligible": [
            {
                "id": item["id"],
                "version": item["version"],
                "scope_specificity": _scope_specificity(item["scope"]),
            }
            for item in selected
        ],
        "needs_revalidation": needs_revalidation,
        "blocked_conflicts": [
            {
                "experience_ids": list(pair),
                "reason": "declared_conflict",
            }
            for pair in sorted(conflict_pairs)
        ],
        "runtime_hints": [deepcopy(item["runtime_hint"]) for item in selected],
    }


def candidate_trigger(
    *,
    experience_ids: Sequence[str],
    failure_clusters: Mapping[str, Sequence[str]],
    severe_event: bool,
) -> dict[str, Any]:
    """Decide whether accumulated evidence warrants candidate reflection."""

    experiences = _safe_id_list(
        experience_ids,
        "candidate trigger experience_ids",
    )
    if not experiences:
        raise ImprovementControlError(
            "candidate trigger requires at least one experience"
        )
    if type(severe_event) is not bool:
        raise ImprovementControlError("candidate trigger severe_event must be boolean")
    experience_set = set(experiences)
    cluster_mapping = _require_mapping(
        failure_clusters,
        "candidate trigger failure_clusters",
    )
    cluster_counts: dict[str, int] = {}
    for raw_cluster_id, raw_members in cluster_mapping.items():
        cluster_id = _safe_id(raw_cluster_id, "candidate trigger cluster id")
        members = _safe_id_list(
            raw_members,
            f"candidate trigger cluster {cluster_id}",
        )
        unknown = sorted(set(members).difference(experience_set))
        if unknown:
            raise ImprovementControlError(
                f"candidate trigger cluster {cluster_id} has unknown experiences: "
                f"{', '.join(unknown)}"
            )
        cluster_counts[cluster_id] = len(members)

    reasons: list[str] = []
    if len(experiences) >= 10:
        reasons.append("experience_batch_threshold")
    if any(count >= 3 for count in cluster_counts.values()):
        reasons.append("failure_cluster_threshold")
    if severe_event:
        reasons.append("severe_event")
    return {
        "triggered": bool(reasons),
        "reason_codes": reasons,
        "experience_count": len(experiences),
        "cluster_counts": {
            key: cluster_counts[key]
            for key in sorted(cluster_counts)
        },
        "severe_event": severe_event,
    }


def candidate_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 fingerprint for one normalized candidate."""

    normalized = _json_mapping(payload, "improvement candidate fingerprint")
    canonical = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def validate_improvement_candidate(
    payload: Mapping[str, Any],
    *,
    rejected_fingerprints: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate one bounded L0-L3 candidate and reject known failed proposals."""

    record = _json_mapping(payload, "improvement candidate")
    _reject_candidate_forbidden_fields(record)
    _require_exact_fields(
        record,
        CANDIDATE_ROOT_FIELDS,
        "improvement candidate",
    )
    if record.get("schema_version") != 1:
        raise ImprovementControlError(
            "improvement candidate schema_version must be 1"
        )
    if record.get("artifact_kind") != "improvement_candidate":
        raise ImprovementControlError(
            "improvement candidate artifact_kind is invalid"
        )

    candidate_id = _safe_id(record.get("candidate_id"), "candidate_id")
    level = _enum(
        record.get("level"),
        set(CANDIDATE_SURFACE_BY_LEVEL),
        "candidate level",
    )
    hypothesis_value = record.get("hypothesis")
    if not isinstance(hypothesis_value, str) or not hypothesis_value.strip():
        raise ImprovementControlError(
            "candidate hypothesis must be one non-empty string"
        )
    hypothesis = hypothesis_value.strip()
    target_failure_cluster = _safe_id_list(
        record.get("target_failure_cluster"),
        "candidate target_failure_cluster",
    )
    if not target_failure_cluster:
        raise ImprovementControlError(
            "candidate target_failure_cluster must be non-empty"
        )
    change_surface_value = record.get("change_surface")
    if not isinstance(change_surface_value, str):
        raise ImprovementControlError(
            "candidate change_surface must be one string"
        )
    change_surface = _enum(
        change_surface_value,
        set(CANDIDATE_SURFACE_BY_LEVEL.values()),
        "candidate change_surface",
    )
    expected_surface = CANDIDATE_SURFACE_BY_LEVEL[level]
    if change_surface != expected_surface:
        raise ImprovementControlError(
            f"candidate change_surface for {level} must be {expected_surface}"
        )
    experience_ids = _safe_id_list(
        record.get("experience_ids"),
        "candidate experience_ids",
    )
    if not experience_ids:
        raise ImprovementControlError("candidate experience_ids must be non-empty")

    activation = _require_mapping(record.get("activation"), "candidate activation")
    _require_exact_fields(
        activation,
        CANDIDATE_ACTIVATION_FIELDS,
        "candidate activation",
    )
    triggers = _unique_string_list(
        activation.get("triggers"),
        "candidate activation triggers",
    )
    if not triggers:
        raise ImprovementControlError(
            "candidate activation triggers must be non-empty"
        )
    prerequisites = _unique_string_list(
        activation.get("prerequisites"),
        "candidate activation prerequisites",
    )
    contraindications = _unique_string_list(
        activation.get("contraindications"),
        "candidate activation contraindications",
    )
    fallback = str(activation.get("fallback") or "").strip()
    if not fallback:
        raise ImprovementControlError(
            "candidate activation fallback must be non-empty"
        )

    bounds = _require_mapping(record.get("bounds"), "candidate bounds")
    _require_exact_fields(bounds, CANDIDATE_BOUNDS_FIELDS, "candidate bounds")
    max_fanout = _bounded_int(
        bounds.get("max_fanout"),
        minimum=1,
        maximum=4,
        field="candidate bounds max_fanout",
    )
    max_retries = _bounded_int(
        bounds.get("max_retries"),
        minimum=0,
        maximum=3,
        field="candidate bounds max_retries",
    )
    token_budget = _bounded_int(
        bounds.get("token_budget"),
        minimum=1,
        maximum=None,
        field="candidate bounds token_budget",
    )

    profile_value = record.get("profile")
    if level == "L3":
        try:
            profile = validate_collaboration_profile(profile_value)
        except CollaborationProfileError as exc:
            raise ImprovementControlError(
                f"candidate profile is invalid: {exc}"
            ) from exc
    else:
        if profile_value is not None:
            raise ImprovementControlError(
                "candidate profile must be null outside L3"
            )
        profile = None

    rollback = _require_mapping(record.get("rollback"), "candidate rollback")
    _require_exact_fields(
        rollback,
        CANDIDATE_ROLLBACK_FIELDS,
        "candidate rollback",
    )
    previous_version = str(rollback.get("previous_version") or "").strip()
    if not previous_version:
        raise ImprovementControlError(
            "candidate rollback previous_version must be non-empty"
        )
    risk_class = _enum(
        record.get("risk_class"),
        {"low", "medium", "high"},
        "candidate risk_class",
    )

    normalized = {
        "schema_version": 1,
        "artifact_kind": "improvement_candidate",
        "candidate_id": candidate_id,
        "level": level,
        "hypothesis": hypothesis,
        "target_failure_cluster": target_failure_cluster,
        "change_surface": change_surface,
        "experience_ids": experience_ids,
        "activation": {
            "triggers": triggers,
            "prerequisites": prerequisites,
            "contraindications": contraindications,
            "fallback": fallback,
        },
        "bounds": {
            "max_fanout": max_fanout,
            "max_retries": max_retries,
            "token_budget": token_budget,
        },
        "profile": profile,
        "rollback": {
            "previous_version": previous_version,
        },
        "risk_class": risk_class,
    }
    fingerprint = candidate_fingerprint(normalized)
    rejected = {
        _sha256(value, "rejected candidate fingerprint")
        for value in _sequence_values(
            rejected_fingerprints,
            "rejected candidate fingerprints",
        )
    }
    if fingerprint in rejected:
        raise ImprovementControlError(
            f"candidate fingerprint was previously rejected: {fingerprint}"
        )
    return {
        "candidate": normalized,
        "fingerprint": fingerprint,
    }


def plan_release_transition(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return an allowed/blocked release plan without performing a transition."""

    record = _json_mapping(payload, "improvement release plan")
    _require_exact_fields(
        record,
        RELEASE_INPUT_FIELDS,
        "improvement release plan",
    )
    if record.get("schema_version") != 1:
        raise ImprovementControlError(
            "improvement release plan schema_version must be 1"
        )
    if record.get("artifact_kind") != "improvement_release_plan":
        raise ImprovementControlError(
            "improvement release plan artifact_kind is invalid"
        )

    reasons: list[str] = []
    release_id = str(record.get("release_id") or "").strip()
    if SAFE_ID_RE.fullmatch(release_id) is None:
        reasons.append("release_lineage_required")
    candidate_id = str(record.get("candidate_id") or "").strip()
    if SAFE_ID_RE.fullmatch(candidate_id) is None:
        reasons.append("candidate_lineage_required")
    current_state = _enum(
        record.get("current_state"),
        RELEASE_STATES,
        "release current_state",
    )
    requested_state = _enum(
        record.get("requested_state"),
        REQUESTED_RELEASE_STATES,
        "release requested_state",
    )
    automatic_promotion = _strict_bool(
        record.get("automatic_promotion"),
        "release automatic_promotion",
    )
    automatic_emergency_disable = _strict_bool(
        record.get("automatic_emergency_disable"),
        "release automatic_emergency_disable",
    )
    operator_approval = _strict_bool(
        record.get("operator_approval"),
        "release operator_approval",
    )
    evidence_refs = _unique_string_list(
        record.get("evidence_refs"),
        "release evidence_refs",
    )
    rollback_version = str(record.get("rollback_version") or "").strip()
    hard_failure, hard_failure_reasons = _normalize_hard_invariant_failure(
        record.get("hard_invariant_failure")
    )
    reasons.extend(hard_failure_reasons)

    if automatic_promotion:
        reasons.append(
            "automatic_forward_promotion_forbidden"
            if requested_state in FORWARD_RELEASE_EVIDENCE
            else "automatic_promotion_must_be_false"
        )

    if requested_state in FORWARD_RELEASE_EVIDENCE:
        expected_current, evidence_prefix, missing_evidence_reason = (
            FORWARD_RELEASE_EVIDENCE[requested_state]
        )
        if current_state != expected_current:
            reasons.append("invalid_state_transition")
        if not operator_approval:
            reasons.append("operator_approval_required")
        if not any(
            reference.startswith(evidence_prefix)
            and len(reference) > len(evidence_prefix)
            for reference in evidence_refs
        ):
            reasons.append(missing_evidence_reason)
    elif requested_state == "quarantined":
        if current_state not in {"shadow", "canary", "active"}:
            reasons.append("invalid_state_transition")
        if not operator_approval:
            if not automatic_emergency_disable:
                reasons.append("automatic_emergency_disable_required")
            if hard_failure is None:
                reasons.append("hard_invariant_evidence_required")
    else:
        if current_state not in {"shadow", "canary", "active", "quarantined"}:
            reasons.append("invalid_state_transition")
        if not rollback_version:
            reasons.append("rollback_version_required")
        if not evidence_refs:
            reasons.append("evidence_lineage_required")
        if hard_failure is not None and (
            hard_failure["evidence_ref"] not in evidence_refs
        ):
            reasons.append("evidence_lineage_required")
        if not operator_approval:
            if not automatic_emergency_disable:
                reasons.append("automatic_emergency_disable_required")
            if hard_failure is None:
                reasons.append("hard_invariant_evidence_required")

    reason_codes = list(dict.fromkeys(reasons))
    return {
        "schema_version": 1,
        "artifact_kind": "improvement_release_plan",
        "release_id": release_id,
        "candidate_id": candidate_id,
        "current_state": current_state,
        "requested_state": requested_state,
        "automatic_promotion": automatic_promotion,
        "automatic_emergency_disable": automatic_emergency_disable,
        "operator_approval": operator_approval,
        "evidence_refs": evidence_refs,
        "rollback_version": rollback_version,
        "hard_invariant_failure": hard_failure,
        "decision": "blocked" if reason_codes else "allowed",
        "reason_codes": reason_codes,
    }


def _validate_create_plan(plan: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(plan, Mapping):
        raise ImprovementControlError("issue plan must be a mapping")
    if plan.get("schema_version") != 1 or plan.get("artifact_kind") != ISSUE_ARTIFACT_KIND:
        raise ImprovementControlError("invalid issue plan contract")
    if plan.get("action") != "create":
        raise ImprovementControlError("apply requires an evidence-complete create plan")

    fields = (
        "title",
        "source_path",
        "source_commit",
        "source_url",
        "source_marker",
        "idempotency_key",
        "body",
    )
    normalized = {field: str(plan.get(field) or "").strip() for field in fields}
    if any(not value for value in normalized.values()):
        raise ImprovementControlError("create plan is missing required evidence")
    if plan.get("labels") != ["bug"]:
        raise ImprovementControlError("create plan must use only the bug label")
    if not SOURCE_COMMIT_RE.fullmatch(normalized["source_commit"]):
        raise ImprovementControlError("create plan source_commit is invalid")
    normalized["source_path"] = _relative_reference(
        normalized["source_path"],
        "create plan source_path",
    )
    source_parts = PurePosixPath(normalized["source_path"]).parts
    if tuple(source_parts[:2]) != ISSUE_LEDGER_PREFIX:
        raise ImprovementControlError(
            "create plan source_path must stay inside the bug issue ledger"
        )
    remote_url = _remote_url_from_source_url(
        normalized["source_url"],
        normalized["source_commit"],
    )
    expected_source_url = (
        f"{remote_url}/blob/{normalized['source_commit']}/"
        f"{quote(normalized['source_path'], safe='/')}"
    )
    if normalized["source_url"] != expected_source_url:
        raise ImprovementControlError(
            "create plan source URL does not match source_path"
        )
    source_name = PurePosixPath(normalized["source_path"]).name
    expected_marker = f"<!-- vao-ledger-source:{source_name} -->"
    if normalized["source_marker"] != expected_marker:
        raise ImprovementControlError("create plan source marker is invalid")
    expected_key = hashlib.sha256(
        f"{expected_marker}\0{normalized['source_commit']}".encode("utf-8")
    ).hexdigest()
    if normalized["idempotency_key"] != expected_key:
        raise ImprovementControlError("create plan idempotency key is stale")
    if expected_marker not in normalized["body"]:
        raise ImprovementControlError("create plan body is missing source marker")
    if normalized["source_url"] not in normalized["body"]:
        raise ImprovementControlError("create plan body is missing source URL")
    return normalized


def _frontmatter_title(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ImprovementControlError("issue source must start with frontmatter")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() == "title":
            title = value.strip()
            if len(title) >= 2 and title[:1] == title[-1:] and title[0] in {'"', "'"}:
                title = title[1:-1].strip()
            if title:
                return title
    raise ImprovementControlError("issue source frontmatter requires a title")


def _canonical_remote_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or len(parts) != 2
        or parsed.query
        or parsed.fragment
    ):
        raise ImprovementControlError(
            "remote_url must be https://github.com/OWNER/REPO"
        )
    repository = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not parts[0] or not repository:
        raise ImprovementControlError("remote_url owner and repository are required")
    return urlunsplit(("https", "github.com", f"/{parts[0]}/{repository}", "", ""))


def _remote_url_from_source_url(source_url: str, source_commit: str) -> str:
    marker = f"/blob/{source_commit}/"
    remote, separator, source_path = source_url.partition(marker)
    if not separator or not source_path:
        raise ImprovementControlError("create plan source URL is invalid")
    return _canonical_remote_url(remote)


def _remote_issue_decision(
    *,
    title: str,
    source_marker: str,
    remote_issues: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, Any] | None, list[str]]:
    if not isinstance(remote_issues, Sequence) or isinstance(
        remote_issues,
        (str, bytes, bytearray),
    ):
        raise ImprovementControlError("remote issues must be a sequence")
    rows: list[Mapping[str, Any]] = []
    for issue in remote_issues:
        if not isinstance(issue, Mapping):
            raise ImprovementControlError("each remote issue must be a mapping")
        rows.append(issue)

    exact = [issue for issue in rows if source_marker in str(issue.get("body") or "")]
    if exact:
        match = min(exact, key=_remote_issue_sort_key)
        return "duplicate", _matched_issue(match), ["exact_source_marker"]

    normalized_title = _normalize_title(title)
    title_matches = [
        issue
        for issue in rows
        if _normalize_title(str(issue.get("title") or "")) == normalized_title
    ]
    if title_matches:
        match = min(title_matches, key=_remote_issue_sort_key)
        return (
            "needs_human",
            _matched_issue(match),
            ["normalized_title_match_without_marker"],
        )
    return "create", None, []


def _normalize_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _remote_issue_sort_key(issue: Mapping[str, Any]) -> tuple[int, str]:
    number = issue.get("number")
    numeric = number if isinstance(number, int) and not isinstance(number, bool) else 2**63
    return numeric, str(issue.get("html_url") or issue.get("url") or "")


def _matched_issue(issue: Mapping[str, Any]) -> dict[str, Any]:
    number = issue.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ImprovementControlError("matched remote issue number is invalid")
    state = str(issue.get("state") or "").strip()
    if state not in {"open", "closed"}:
        raise ImprovementControlError("matched remote issue state is invalid")
    return {
        "number": number,
        "state": state,
        "url": str(issue.get("html_url") or issue.get("url") or ""),
    }


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ImprovementControlError(f"{field} must be a mapping")
    return value


def _json_mapping(value: object, field: str) -> dict[str, Any]:
    mapping = _require_mapping(value, field)
    try:
        return json.loads(
            json.dumps(
                dict(mapping),
                allow_nan=False,
                ensure_ascii=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ImprovementControlError(f"{field} must contain JSON values") from exc


def _safe_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if SAFE_ID_RE.fullmatch(text) is None:
        raise ImprovementControlError(f"{field} is invalid")
    return text


def _enum(value: object, allowed: set[str], field: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ImprovementControlError(f"{field} is invalid")
    return text


def _sha256(value: object, field: str) -> str:
    text = str(value or "").strip()
    if SHA256_RE.fullmatch(text) is None:
        raise ImprovementControlError(f"{field} must be sha256: plus 64 lowercase hex")
    return text


def _relative_reference(value: object, field: str) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
    ):
        raise ImprovementControlError(f"{field} must be a repository-relative path")
    return path.as_posix()


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ImprovementControlError(f"{field} must be a list")
    result = [str(item or "").strip() for item in value]
    if any(not item for item in result):
        raise ImprovementControlError(f"{field} entries must be non-empty")
    return result


def _evidence_reference(value: object, field: str) -> dict[str, str]:
    mapping = _require_mapping(value, f"episode {field}")
    reference = str(mapping.get("ref") or "").strip()
    if not reference:
        raise ImprovementControlError(f"episode {field} ref is required")
    return {
        "ref": reference,
        "sha256": _sha256(
            mapping.get("sha256"),
            f"episode {field} sha256",
        ),
    }


def _experience_evidence_references(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ImprovementControlError(
            "experience evidence_refs must be a list"
        )
    references: list[dict[str, str]] = []
    for index, item in enumerate(value):
        mapping = _require_mapping(
            item,
            f"experience evidence_refs[{index}]",
        )
        _require_exact_fields(
            mapping,
            frozenset({"ref", "sha256"}),
            f"experience evidence_refs[{index}]",
        )
        reference = str(mapping.get("ref") or "").strip()
        if not reference:
            raise ImprovementControlError(
                f"experience evidence_refs[{index}] ref is required"
            )
        references.append(
            {
                "ref": reference,
                "sha256": _sha256(
                    mapping.get("sha256"),
                    f"experience evidence_refs[{index}] sha256",
                ),
            }
        )
    fingerprints = [
        (item["ref"], item["sha256"])
        for item in references
    ]
    if len(set(fingerprints)) != len(fingerprints):
        raise ImprovementControlError(
            "experience evidence_refs entries must be unique"
        )
    return references


def _live_evidence(value: object) -> dict[str, Any]:
    mapping = _require_mapping(value, "episode live evidence")
    applicable = mapping.get("applicable")
    if type(applicable) is not bool:
        raise ImprovementControlError("episode live applicable must be boolean")
    reference = str(mapping.get("ref") or "").strip()
    sha256 = str(mapping.get("sha256") or "").strip()
    reason = str(mapping.get("not_applicable_reason") or "").strip()
    if applicable:
        if not reference:
            raise ImprovementControlError("applicable live evidence requires ref")
        sha256 = _sha256(sha256, "episode live sha256")
        if reason:
            raise ImprovementControlError(
                "applicable live evidence must not have not_applicable_reason"
            )
    elif reference or sha256 or not reason:
        raise ImprovementControlError(
            "non-applicable live evidence requires only a reason"
        )
    return {
        "applicable": applicable,
        "ref": reference,
        "sha256": sha256,
        "not_applicable_reason": reason,
    }


def _episode_lineage(value: object) -> dict[str, list[str]]:
    mapping = _require_mapping(value, "episode lineage")
    return {
        field: [
            _safe_id(item, f"episode lineage {field}")
            for item in _require_id_list(mapping.get(field), f"episode lineage {field}")
        ]
        for field in ("experience_ids", "candidate_ids", "release_ids")
    }


def _experience_lineage(value: object) -> dict[str, list[str]]:
    mapping = _require_mapping(value, "experience lineage")
    return {
        field: [
            _safe_id(item, f"experience lineage {field}")
            for item in _require_id_list(
                mapping.get(field),
                f"experience lineage {field}",
            )
        ]
        for field in ("supersedes", "conflicts_with", "derived_candidates")
    }


def _require_id_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ImprovementControlError(f"{field} must be a list")
    return value


def _runtime_hint(value: object) -> dict[str, str]:
    mapping = _require_mapping(value, "experience runtime_hint")
    return {
        field: str(mapping.get(field) or "").strip()
        for field in ("condition", "action", "constraint", "fallback")
    }


def _require_exact_fields(
    mapping: Mapping[str, Any],
    expected: frozenset[str],
    field: str,
) -> None:
    observed = set(mapping)
    unknown = sorted(observed.difference(expected))
    if unknown:
        raise ImprovementControlError(
            f"{field} has unknown fields: {', '.join(unknown)}"
        )
    missing = sorted(expected.difference(observed))
    if missing:
        raise ImprovementControlError(
            f"{field} is missing fields: {', '.join(missing)}"
        )


def _reject_candidate_forbidden_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in FORBIDDEN_FIELDS:
                raise ImprovementControlError(
                    f"improvement candidate must not contain {key}"
                )
            _reject_candidate_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_candidate_forbidden_fields(nested)


def _sequence_values(value: object, field: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ImprovementControlError(f"{field} must be a sequence")
    return list(value)


def _safe_id_list(value: object, field: str) -> list[str]:
    result = [
        _safe_id(item, field)
        for item in _sequence_values(value, field)
    ]
    if len(set(result)) != len(result):
        raise ImprovementControlError(f"{field} entries must be unique")
    return result


def _unique_string_list(value: object, field: str) -> list[str]:
    result = _string_list(value, field)
    if len(set(result)) != len(result):
        raise ImprovementControlError(f"{field} entries must be unique")
    return result


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int | None,
    field: str,
) -> int:
    if type(value) is not int or value < minimum:
        raise ImprovementControlError(f"{field} is outside the allowed bounds")
    if maximum is not None and value > maximum:
        raise ImprovementControlError(f"{field} is outside the allowed bounds")
    return value


def _strict_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ImprovementControlError(f"{field} must be boolean")
    return value


def _normalize_hard_invariant_failure(
    value: object,
) -> tuple[dict[str, str] | None, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, Mapping):
        return None, ["hard_invariant_contract_invalid"]
    mapping = _json_mapping(value, "hard_invariant_failure")
    reasons: list[str] = []
    if set(mapping) != HARD_INVARIANT_FIELDS:
        reasons.append("hard_invariant_contract_invalid")
    invariant_id = str(mapping.get("invariant_id") or "").strip()
    evidence_ref = str(mapping.get("evidence_ref") or "").strip()
    evidence_sha256 = str(mapping.get("evidence_sha256") or "").strip()
    if invariant_id not in HARD_INVARIANT_IDS:
        reasons.append("hard_invariant_id_invalid")
    if not evidence_ref:
        reasons.append("hard_invariant_evidence_ref_required")
    if SHA256_RE.fullmatch(evidence_sha256) is None:
        reasons.append("hard_invariant_evidence_sha256_invalid")
    return (
        {
            "invariant_id": invariant_id,
            "evidence_ref": evidence_ref,
            "evidence_sha256": evidence_sha256,
        },
        reasons,
    )


def _scope_matches(required: object, observed: object) -> bool:
    if isinstance(required, Mapping):
        if not isinstance(observed, Mapping):
            return False
        return all(
            key in observed and _scope_matches(value, observed[key])
            for key, value in required.items()
        )
    if isinstance(required, list):
        if not isinstance(observed, list):
            return False
        return all(value in observed for value in required)
    return required == observed


def _scope_specificity(value: object) -> int:
    if isinstance(value, Mapping):
        return sum(
            1 + _scope_specificity(nested)
            for nested in value.values()
        )
    if isinstance(value, list):
        return sum(1 + _scope_specificity(nested) for nested in value)
    return 1


def _bind_claim_store(claim_store: Path, remote_url: str) -> None:
    claim_store.mkdir(parents=True, exist_ok=True)
    identity_path = claim_store / "remote-identity.json"
    identity = {
        "schema_version": 1,
        "artifact_kind": "improvement_issue_claim_store_identity",
        "remote_url": remote_url,
        "remote_sha256": hashlib.sha256(remote_url.encode("utf-8")).hexdigest(),
    }
    try:
        _write_exclusive_json(identity_path, identity)
        return
    except FileExistsError:
        pass
    try:
        observed = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImprovementControlError(
            "claim store remote identity is unreadable"
        ) from exc
    if observed != identity:
        raise ImprovementControlError(
            "claim store remote identity does not match create plan"
        )


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = _json_bytes(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
