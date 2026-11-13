"""Worker-safe AG2 meeting request profiles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .presets import resolve_meeting_preset

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MEETING_REQUEST_PROFILE_FIELDS = frozenset(
    {
        "name",
        "preset",
        "max_rounds",
        "allow_dry_run_proposal",
        "allowed_workers",
        "audit_level",
        "timeout_seconds",
        "max_cost_usd",
        "empty_response_retries",
    }
)
FORBIDDEN_PROFILE_FIELDS = frozenset(
    {
        "runtime",
        "real_runtime_mode",
        "allow_real_model_call",
        "ag2_effective_approval_json",
        "ag2_runtime_config_json",
        "approval_json",
        "runtime_config_json",
        "host_local_path",
        "provider",
        "model",
        "base_url",
        "api_key",
        "api_key_env",
        "endpoint",
    }
)


@dataclass(frozen=True)
class MeetingRequestProfile:
    name: str
    preset: str
    max_rounds: int
    allow_dry_run_proposal: bool = False
    allowed_workers: tuple[str, ...] = ()
    audit_level: str = "standard"
    timeout_seconds: int = 60
    max_cost_usd: float = 0.05
    empty_response_retries: int = 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "profile_kind": "ag2_meeting_request_profile",
            "name": self.name,
            "preset": self.preset,
            "max_rounds": self.max_rounds,
            "allow_dry_run_proposal": self.allow_dry_run_proposal,
            "allowed_workers": list(self.allowed_workers),
            "audit_level": self.audit_level,
            "timeout_seconds": self.timeout_seconds,
            "max_cost_usd": self.max_cost_usd,
            "empty_response_retries": self.empty_response_retries,
            "would_execute_live": False,
            "provider_values_recorded": False,
            "secret_values_recorded": False,
            "host_local_paths_recorded": False,
        }


def load_meeting_request_profile(name: str, *, profile_dir: Path) -> MeetingRequestProfile:
    normalized_name = _required_profile_name(name)
    path = (profile_dir / f"{normalized_name}.json").resolve(strict=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"meeting request profile could not be read: {normalized_name}") from exc
    if not isinstance(payload, dict):
        raise ValueError("meeting request profile must contain a JSON object")
    unsupported = sorted(set(payload) - MEETING_REQUEST_PROFILE_FIELDS)
    forbidden = sorted(set(payload) & FORBIDDEN_PROFILE_FIELDS)
    if unsupported or forbidden:
        fields = ", ".join(sorted(set(unsupported) | set(forbidden)))
        raise ValueError(f"unsupported meeting request profile fields: {fields}")

    payload_name = _required_profile_text(payload.get("name", normalized_name), "name")
    if payload_name != normalized_name:
        raise ValueError("meeting request profile name must match file name")
    preset = _required_profile_text(payload.get("preset"), "preset")
    preset_contract = resolve_meeting_preset(preset)
    max_rounds = payload.get("max_rounds", preset_contract.max_rounds)
    if type(max_rounds) is not int or max_rounds < 1 or max_rounds > preset_contract.max_rounds:
        raise ValueError(f"meeting request profile max_rounds must be between 1 and {preset_contract.max_rounds}")
    allow_dry_run_proposal = payload.get("allow_dry_run_proposal", False)
    if type(allow_dry_run_proposal) is not bool:
        raise ValueError("meeting request profile allow_dry_run_proposal must be boolean")
    allowed_workers = _normalize_worker_tuple(payload.get("allowed_workers", ()))
    audit_level = _required_profile_text(payload.get("audit_level", "standard"), "audit_level")
    if audit_level not in {"minimal", "standard", "strict"}:
        raise ValueError("meeting request profile audit_level must be minimal, standard, or strict")
    timeout_seconds = payload.get("timeout_seconds", 60)
    if type(timeout_seconds) is not int or timeout_seconds < 1 or timeout_seconds > 300:
        raise ValueError("meeting request profile timeout_seconds must be an integer between 1 and 300")
    max_cost_usd = payload.get("max_cost_usd", 0.05)
    if type(max_cost_usd) not in {int, float} or isinstance(max_cost_usd, bool) or max_cost_usd <= 0 or max_cost_usd > 0.1:
        raise ValueError("meeting request profile max_cost_usd must be a positive number no greater than 0.1")
    empty_response_retries = payload.get("empty_response_retries", 1)
    if type(empty_response_retries) is not int or empty_response_retries < 0 or empty_response_retries > 3:
        raise ValueError("meeting request profile empty_response_retries must be an integer between 0 and 3")
    if allow_dry_run_proposal and not allowed_workers:
        raise ValueError("meeting request profile allowed_workers is required when dry-run proposals are allowed")
    return MeetingRequestProfile(
        name=normalized_name,
        preset=preset,
        max_rounds=max_rounds,
        allow_dry_run_proposal=allow_dry_run_proposal,
        allowed_workers=allowed_workers,
        audit_level=audit_level,
        timeout_seconds=timeout_seconds,
        max_cost_usd=float(max_cost_usd),
        empty_response_retries=empty_response_retries,
    )


def _required_profile_name(value: str) -> str:
    if not isinstance(value, str) or not PROFILE_NAME_RE.fullmatch(value.strip()):
        raise ValueError("meeting request profile name must be a lowercase slug")
    return value.strip()


def _required_profile_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"meeting request profile {field_name} must be a non-empty string")
    return value.strip()


def _normalize_worker_tuple(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ValueError("meeting request profile allowed_workers must be a list")
    workers: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("meeting request profile allowed_workers must contain non-empty strings")
        workers.append(item.strip())
    if len(set(workers)) != len(workers):
        raise ValueError("meeting request profile allowed_workers must not contain duplicates")
    return tuple(workers)
