"""Phase-2 Jev shadow advisory: one bounded decision point.

Asks, for one orchestration decision that already has coder and reviewer
materials, whether an additional read-only Astra deep review is worth it.
The tool is observation-only: it never dispatches work, touches locks or
terminal state, or connects to OpenCode/OMO internals. The host keeps the
final decision; an unavailable service never becomes an outer-task failure.
"""

from __future__ import annotations

import hashlib
import http.client as http_client
import json
import math
import os
import time
import urllib.error as urllib_error
import urllib.request as urllib_request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TOOL_NAME = "omx_jev_shadow"
DECISION_POINT = "post_review_astra_value"
QUESTION_ID = "astra_review_value"
QUESTION_VERSION = "jev-shadow-astra-review-v1"
JEV_MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
CHOICES = ("routine", "gather_evidence", "request_astra")
MAX_INPUT_BYTES = 12 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 20.0
PROBABILITY_SUM_TOLERANCE = 1e-6
ALLOWED_CASE_KEYS = frozenset({"case_id", "evidence_version", "context", "sol_baseline"})
UNAVAILABLE_REASONS = frozenset(
    {
        "missing_api_key",
        "invalid_api_key",
        "timeout",
        "http_error",
        "network_error",
        "model_mismatch",
        "response_too_large",
        "malformed_response",
    }
)
_OBSERVATION_NOTES = (
    "advisory-only: the host decides; contracted must-Astra stages are executed directly and Jev is not a gate",
    "agreement is not correctness; confidence is not a measured accuracy",
    "actual_cost unknown; no savings claim from single-call prices",
)

_INSTRUCTIONS = (
    "Advisory schema: "
    + QUESTION_VERSION
    + ". You are a shadow advisor for one orchestration decision. The state "
    "is the decision context with the existing coder and reviewer materials. "
    "Judge only whether one additional bounded read-only Astra deep review "
    "would add real value for this specific decision. An existing "
    "still-valid opinion that semantically covers the question, or an "
    "ordinary small revision, adds no value. A key architecture tradeoff, a "
    "substantive coder/reviewer disagreement, or a contracted Astra stage "
    "does. One confirmed problem still failing after two actual repairs is a "
    "reminder to consider diagnosis, not a circuit breaker on any two tool "
    "errors; re-running the same failing command or environment jitter does "
    "not count. If a known, specific input required to answer this exact "
    "question is missing, gather it first; but a question that itself "
    "diagnoses an unknown cause or an evidence gap may warrant the review "
    "without that diagnosis first. This advice is observation-only and never "
    "dispatches work or terminal state."
)

_CRITERIA_TEXT = {
    "routine": (
        "Existing coder/reviewer materials already cover this decision "
        "(ordinary small revision, or a still-valid prior opinion is "
        "semantically reusable); an extra Astra review adds no material value."
    ),
    "gather_evidence": (
        "A known, specific input required to answer this exact question is "
        "missing and nameable; collecting it comes before any deep review. "
        "Diagnosing an unknown cause is not this option."
    ),
    "request_astra": (
        "A bounded read-only Astra review adds real value now: key "
        "architecture tradeoff, substantive coder/reviewer disagreement, "
        "contracted Astra stage, or diagnosis of one confirmed problem still "
        "failing after two actual repairs."
    ),
}


class JevShadowError(ValueError):
    """Operator-facing local input or configuration error."""


class JevShadowUnavailable(Exception):
    """Remote call unavailable; carries only a safe reason class."""

    def __init__(self, reason: str) -> None:
        if reason not in UNAVAILABLE_REASONS:
            raise JevShadowError(f"unknown unavailable reason: {reason}")
        super().__init__(reason)
        self.reason = reason


Transport = Callable[[bytes, str, float], bytes]


def load_shadow_case(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise JevShadowError(f"case input not found: {path}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise JevShadowError("case input is not readable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise JevShadowError(
            f"case input exceeds {MAX_INPUT_BYTES} bytes: {len(raw)}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise JevShadowError("case input is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise JevShadowError("case input must be a JSON object")
    unknown = set(payload) - set(ALLOWED_CASE_KEYS)
    if unknown:
        raise JevShadowError(f"unknown case keys: {sorted(unknown)}")
    for field in ("case_id", "evidence_version", "context"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JevShadowError(f"case field must be a non-empty string: {field}")
    baseline = payload.get("sol_baseline")
    if baseline is not None and baseline not in CHOICES:
        raise JevShadowError(
            "sol_baseline must be one of " + ", ".join(CHOICES) + " when present"
        )
    return payload


def build_request_payload(
    case: Mapping[str, Any], *, reverse_options: bool = False
) -> dict[str, Any]:
    order = list(CHOICES)
    if reverse_options:
        order.reverse()
    criteria: dict[str, str] = {choice: _CRITERIA_TEXT[choice] for choice in order}
    return {
        "state": case["context"],
        "model": JEV_MODEL,
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": _INSTRUCTIONS,
                "criteria": criteria,
            }
        },
    }


def _unit_interval_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= 1
    if isinstance(value, float):
        return math.isfinite(value) and 0.0 <= value <= 1.0
    return False


def _nonneg_int_tokens(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_response(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JevShadowUnavailable("response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise JevShadowUnavailable("malformed_response") from exc
    if not isinstance(payload, dict):
        raise JevShadowUnavailable("malformed_response")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise JevShadowUnavailable("malformed_response")
    answers = payload.get("answers")
    if not isinstance(answers, dict) or QUESTION_ID not in answers:
        raise JevShadowUnavailable("malformed_response")
    answer = answers[QUESTION_ID]
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevShadowUnavailable("malformed_response")
    choice = answer.get("choice")
    if choice not in CHOICES:
        raise JevShadowUnavailable("malformed_response")
    probabilities_raw = answer.get("probabilities")
    if not isinstance(probabilities_raw, dict):
        raise JevShadowUnavailable("malformed_response")
    if set(probabilities_raw) != set(CHOICES):
        raise JevShadowUnavailable("malformed_response")
    for value in probabilities_raw.values():
        if not _unit_interval_number(value):
            raise JevShadowUnavailable("malformed_response")
    total = sum(probabilities_raw.values())
    if abs(total - 1) > PROBABILITY_SUM_TOLERANCE:
        raise JevShadowUnavailable("malformed_response")
    if probabilities_raw[choice] != max(probabilities_raw.values()):
        raise JevShadowUnavailable("malformed_response")
    confidence = answer.get("confidence")
    if not _unit_interval_number(confidence):
        raise JevShadowUnavailable("malformed_response")
    usage_raw = payload.get("usage")
    if not isinstance(usage_raw, dict):
        raise JevShadowUnavailable("malformed_response")
    if not _nonneg_int_tokens(usage_raw.get("input_tokens")) or not _nonneg_int_tokens(
        usage_raw.get("output_tokens")
    ):
        raise JevShadowUnavailable("malformed_response")
    usage = {
        "input_tokens": usage_raw["input_tokens"],
        "output_tokens": usage_raw["output_tokens"],
    }
    return {
        "model_reported": model,
        "advice": {
            "choice": choice,
            "probabilities": dict(probabilities_raw),
            "confidence": confidence,
        },
        "usage": usage,
    }


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_safe_header_value(value: str) -> bool:
    return bool(value) and all(33 <= ord(ch) <= 126 for ch in value)


def urllib_transport(payload: bytes, api_key: str, timeout_seconds: float) -> bytes:
    if not api_key:
        raise JevShadowUnavailable("missing_api_key")
    if not isinstance(api_key, str) or not _is_safe_header_value(api_key):
        raise JevShadowUnavailable("invalid_api_key")
    req = urllib_request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urllib_request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(req, timeout=timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib_error.HTTPError as exc:
        raise JevShadowUnavailable("http_error") from exc
    except TimeoutError as exc:
        raise JevShadowUnavailable("timeout") from exc
    except urllib_error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise JevShadowUnavailable("timeout") from exc
        raise JevShadowUnavailable("network_error") from exc
    except http_client.HTTPException as exc:
        raise JevShadowUnavailable("network_error") from exc
    except OSError as exc:
        raise JevShadowUnavailable("network_error") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JevShadowUnavailable("response_too_large")
    return raw


def _validate_timeout(timeout_seconds: float) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise JevShadowError("timeout must be a finite number")
    if not math.isfinite(timeout_seconds) or not (
        0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise JevShadowError(
            f"timeout must be finite and within (0, {MAX_TIMEOUT_SECONDS}] seconds"
        )


def execute_shadow_case(
    case: Mapping[str, Any],
    *,
    live: bool,
    reverse_options: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Transport | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    _validate_timeout(timeout_seconds)
    baseline = case.get("sol_baseline")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "decision_point": DECISION_POINT,
        "case_id": case["case_id"],
        "evidence_version": case["evidence_version"],
        "question_version": QUESTION_VERSION,
        "model_requested": JEV_MODEL,
        "reverse_options": bool(reverse_options),
        "host_baseline": baseline,
        "notes": list(_OBSERVATION_NOTES),
    }
    payload = build_request_payload(case, reverse_options=reverse_options)
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if not live:
        result["mode"] = "dry-run"
        result["status"] = "dry-run"
        result["request_preview"] = payload
        return result
    result["mode"] = "live"
    result["request_sha256"] = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
    effective_transport = transport or urllib_transport
    effective_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
    started = time.monotonic()
    try:
        raw = effective_transport(payload_bytes, effective_key, timeout_seconds)
        parsed = parse_response(raw)
        if parsed["model_reported"] != JEV_MODEL:
            result["model_reported"] = parsed["model_reported"]
            raise JevShadowUnavailable("model_mismatch")
    except JevShadowUnavailable as exc:
        result["status"] = "unavailable"
        result["unavailable_reason"] = exc.reason
        result["duration_ms"] = max(1, round((time.monotonic() - started) * 1000))
        return result
    except TimeoutError:
        result["status"] = "unavailable"
        result["unavailable_reason"] = "timeout"
        result["duration_ms"] = max(1, round((time.monotonic() - started) * 1000))
        return result
    result["status"] = "advised"
    result["duration_ms"] = max(1, round((time.monotonic() - started) * 1000))
    result.update(parsed)
    result["agreement"] = (
        None
        if baseline is None
        else ("agree" if parsed["advice"]["choice"] == baseline else "disagree")
    )
    return result
