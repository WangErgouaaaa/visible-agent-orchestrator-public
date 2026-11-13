"""User authorization records for future live worker candidates.

This module validates local JSON only. A valid record is evidence that the
user approved a narrowly described future candidate; it is not live enable,
registration, or a substitute for preflight evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


ALLOWED_BACKENDS = {"claude_code", "cursor_agent", "opencode_tui"}


@dataclass(frozen=True)
class UserAuthorizationRecord:
    schema_version: int
    authorization_id: str
    created_at: str
    created_by: str
    authorization_scope: str
    backend_kind: str
    worker_id: str
    worker_label: str
    workspace_root: str
    runtime_backend: str
    runtime_binding_kind: str
    pane_id: str
    process_id: str
    port: str
    user_owned_pane: bool
    allow_user_owned_pane: bool
    allowed_read_roots: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    allow_business_repo_writes: bool = False
    business_repo_path: str = ""
    expected_business_index_files: list[str] = field(default_factory=list)
    task_profile: str = ""
    allowed_task_kinds: list[str] = field(default_factory=list)
    allow_live_submit: bool = False
    allow_registration: bool = False
    allow_single_worker_smoke: bool = False
    allow_mixed_worker: bool = False
    expires_at: str = ""
    revocation_id: str = ""
    operator_notes: str = ""
    risk_acknowledgements: list[str] = field(default_factory=list)
    required_preflight_checks: list[str] = field(default_factory=list)
    required_evidence_paths: list[str] = field(default_factory=list)
    signed_by_user_text: str = ""
    explicit_user_confirmation_text: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "UserAuthorizationRecord":
        return cls(
            schema_version=int(payload.get("schema_version", 0) or 0),
            authorization_id=str(payload.get("authorization_id", "")),
            created_at=str(payload.get("created_at", "")),
            created_by=str(payload.get("created_by", "")),
            authorization_scope=str(payload.get("authorization_scope", "")),
            backend_kind=str(payload.get("backend_kind", "")),
            worker_id=str(payload.get("worker_id", "")),
            worker_label=str(payload.get("worker_label", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            runtime_backend=str(payload.get("runtime_backend", "")),
            runtime_binding_kind=str(payload.get("runtime_binding_kind", "")),
            pane_id=str(payload.get("pane_id", "")),
            process_id=str(payload.get("process_id", "")),
            port=str(payload.get("port", "")),
            user_owned_pane=bool(payload.get("user_owned_pane", False)),
            allow_user_owned_pane=bool(payload.get("allow_user_owned_pane", False)),
            allowed_read_roots=_string_list(payload.get("allowed_read_roots", [])),
            allowed_write_roots=_string_list(payload.get("allowed_write_roots", [])),
            forbidden_paths=_string_list(payload.get("forbidden_paths", [])),
            allow_business_repo_writes=bool(payload.get("allow_business_repo_writes", False)),
            business_repo_path=str(payload.get("business_repo_path", "")),
            expected_business_index_files=_string_list(payload.get("expected_business_index_files", [])),
            task_profile=str(payload.get("task_profile", "")),
            allowed_task_kinds=_string_list(payload.get("allowed_task_kinds", [])),
            allow_live_submit=bool(payload.get("allow_live_submit", False)),
            allow_registration=bool(payload.get("allow_registration", False)),
            allow_single_worker_smoke=bool(payload.get("allow_single_worker_smoke", False)),
            allow_mixed_worker=bool(payload.get("allow_mixed_worker", False)),
            expires_at=str(payload.get("expires_at", "")),
            revocation_id=str(payload.get("revocation_id", "")),
            operator_notes=str(payload.get("operator_notes", "")),
            risk_acknowledgements=_string_list(payload.get("risk_acknowledgements", [])),
            required_preflight_checks=_string_list(payload.get("required_preflight_checks", [])),
            required_evidence_paths=_string_list(payload.get("required_evidence_paths", [])),
            signed_by_user_text=str(payload.get("signed_by_user_text", "")),
            explicit_user_confirmation_text=str(payload.get("explicit_user_confirmation_text", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthorizationValidationResult:
    ok: bool
    authorization_id: str = ""
    authorization_scope: str = ""
    backend_kind: str = ""
    worker_id: str = ""
    workspace_root: str = ""
    allow_live_submit: bool = False
    allow_registration: bool = False
    allow_single_worker_smoke: bool = False
    allow_mixed_worker: bool = False
    allow_business_repo_writes: bool = False
    user_owned_pane: bool = False
    allow_user_owned_pane: bool = False
    blocked_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    safe_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_authorization_record_file(path: Path | str) -> AuthorizationValidationResult:
    record_path = Path(path)
    if record_path.is_dir():
        return _invalid([f"authorization record path is not a file: {record_path}"], evidence_source=str(record_path))
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _invalid([f"authorization record missing: {record_path}"], evidence_source=str(record_path))
    except json.JSONDecodeError as exc:
        return _invalid([f"malformed JSON: {exc.msg}"], evidence_source=str(record_path))
    if not isinstance(payload, dict):
        return _invalid(["authorization record must contain an object"], evidence_source=str(record_path))
    return validate_authorization_record_payload(payload, evidence_source=str(record_path))


def validate_authorization_record_payload(
    payload: dict[str, Any],
    *,
    evidence_source: str = "inline",
) -> AuthorizationValidationResult:
    try:
        record = UserAuthorizationRecord.from_dict(payload)
    except (TypeError, ValueError) as exc:
        return _invalid([f"authorization record fields malformed: {exc}"], evidence_source=evidence_source)

    blocked: list[str] = []
    warnings: list[str] = [
        "authorization record is not live enable",
        "authorization record is not registration",
        "authorization record is not a preflight evidence substitute",
    ]

    if record.backend_kind not in ALLOWED_BACKENDS:
        blocked.append(f"unknown backend_kind: {record.backend_kind}")
    if not record.authorization_id:
        blocked.append("missing authorization_id")
    if not record.authorization_scope:
        blocked.append("missing authorization_scope")
    if not record.worker_id:
        blocked.append("missing worker_id")
    if not record.workspace_root:
        blocked.append("missing workspace_root")
    elif not _is_absolute_path(record.workspace_root):
        blocked.append("workspace_root must be absolute")
    if not record.explicit_user_confirmation_text.strip():
        blocked.append("missing explicit_user_confirmation_text")
    if not record.expires_at:
        blocked.append("missing expires_at")
    else:
        expires = _parse_instant(record.expires_at)
        if expires is None:
            blocked.append("expires_at must be an ISO timestamp")
        elif expires <= datetime.now(timezone.utc):
            blocked.append("authorization expired")
    if record.user_owned_pane and not record.allow_user_owned_pane:
        blocked.append("user_owned_pane=true requires allow_user_owned_pane=true")
    if _is_mixed_scope(record.authorization_scope) and not record.allow_mixed_worker:
        blocked.append("mixed worker authorization requires allow_mixed_worker=true")
    if record.allow_registration:
        warnings.append("allow_registration=true still requires a separate registration gate")
    if record.allow_live_submit:
        warnings.append("allow_live_submit=true still cannot override backend live capability or live enable gate")

    blocked.extend(_write_scope_blocks(record))
    blocked = _dedupe(blocked)
    warnings = _dedupe(warnings)
    ok = not blocked
    return AuthorizationValidationResult(
        ok=ok,
        authorization_id=record.authorization_id,
        authorization_scope=record.authorization_scope,
        backend_kind=record.backend_kind,
        worker_id=record.worker_id,
        workspace_root=record.workspace_root,
        allow_live_submit=record.allow_live_submit,
        allow_registration=record.allow_registration,
        allow_single_worker_smoke=record.allow_single_worker_smoke,
        allow_mixed_worker=record.allow_mixed_worker,
        allow_business_repo_writes=record.allow_business_repo_writes,
        user_owned_pane=record.user_owned_pane,
        allow_user_owned_pane=record.allow_user_owned_pane,
        blocked_reasons=blocked,
        warnings=warnings,
        evidence=[{"source": "authorization_record", "detail": {"evidence_source": evidence_source}}],
        safe_next_actions=_safe_next_actions(ok),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _write_scope_blocks(record: UserAuthorizationRecord) -> list[str]:
    blocked: list[str] = []
    workspace = record.workspace_root
    business = record.business_repo_path
    for write_root in record.allowed_write_roots:
        if business and _path_within(write_root, business) and not record.allow_business_repo_writes:
            blocked.append("business repo write root requires allow_business_repo_writes=true")
        elif workspace and not _path_within(write_root, workspace):
            blocked.append(f"allowed_write_root outside workspace_root: {write_root}")
    return blocked


def _invalid(blocked: list[str], *, evidence_source: str) -> AuthorizationValidationResult:
    return AuthorizationValidationResult(
        ok=False,
        blocked_reasons=_dedupe(blocked),
        warnings=[
            "authorization record is not live enable",
            "authorization record is not registration",
            "authorization record is not a preflight evidence substitute",
        ],
        evidence=[{"source": "authorization_record", "detail": {"evidence_source": evidence_source}}],
        safe_next_actions=_safe_next_actions(False),
        forbidden_next_actions=_forbidden_next_actions(),
    )


def _parse_instant(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_absolute_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute()


def _path_within(path: str, root: str) -> bool:
    if not path or not root:
        return False
    candidate = PurePosixPath(path)
    base = PurePosixPath(root)
    if not candidate.is_absolute() or not base.is_absolute():
        return False
    return candidate == base or base in candidate.parents


def _is_mixed_scope(scope: str) -> bool:
    return "mixed" in scope.replace("-", "_").split("_")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _safe_next_actions(ok: bool) -> list[str]:
    if ok:
        return [
            "rerun fixture-only preflight evidence with this authorization record",
            "prepare separate live enable gate evidence",
            "do not register or submit live workers",
        ]
    return [
        "fix authorization record fields",
        "rerun validate-authorization-record",
        "do not register or submit live workers",
    ]


def _forbidden_next_actions() -> list[str]:
    return [
        "write live registry",
        "write runtime state",
        "touch user-owned Claude Code panel",
        "restore or rebind live workers",
        "use replacement live workers",
        "invoke live agent commands",
        "submit OpenCode live",
        "direct adapter submit",
        "direct prompt mutation endpoints",
    ]


__all__ = [
    "AuthorizationValidationResult",
    "UserAuthorizationRecord",
    "validate_authorization_record_file",
    "validate_authorization_record_payload",
]
