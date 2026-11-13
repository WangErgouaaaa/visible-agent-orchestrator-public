"""Handoff evidence preparation: offline deterministic index plus one
optional bounded Jev claim-support question.

The offline path (default) checks explicitly supplied evidence references —
path under one explicitly selected root, optional line range, optional
per-file sha256, optional root git commit — and emits a compact, attributable
snippet index. It never executes commands embedded in the input, never
searches trees, and keeps the formal report and the full evidence files
untouched at their paths.

The optional live path sends exactly one request to the fixed Jev endpoint
with one bounded question type per claim: whether the claim is supported,
contradicted, or insufficiently addressed by the actually supplied evidence
excerpts. Each question's instructions quote its own claim, so the judgment
never relies on question ids or positional keys. Only deliberately selected
sanitized semantic text is sent; local paths, claim ids, baselines, and
labels stay local. Any unavailability falls back to the offline index
without failing the run. Labels are advisory: they never suppress claims,
accept or reject a delivery, or gate authorization, dispatch, locks, or
terminal state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from .jev_shadow import (
    API_KEY_ENV,
    DEFAULT_TIMEOUT_SECONDS,
    ENDPOINT,
    JEV_MODEL,
    MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_SECONDS,
    _nonneg_int_tokens,
    _unit_interval_number,
    JevShadowUnavailable,
    urllib_transport,
)

SCHEMA_VERSION = 1
TOOL_NAME = "omx_handoff_evidence"
QUESTION_VERSION = "handoff-evidence-claim-support-v1"
SUPPORT_CHOICES = ("supported", "contradicted", "insufficiently_addressed")
MAX_INPUT_BYTES = 256 * 1024
MAX_CLAIMS = 8
MAX_EVIDENCE_PER_CLAIM = 6
MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
MAX_LINE_RANGE_SPAN = 200
MAX_SNIPPET_BYTES = 1200
MAX_STATE_BYTES = 24 * 1024
PROBABILITY_SUM_TOLERANCE = 1e-6
GIT_TIMEOUT_SECONDS = 20

ALLOWED_TOP_KEYS = frozenset({"handoff_id", "root", "expect_commit", "claims"})
ALLOWED_CLAIM_KEYS = frozenset({"claim_id", "text", "evidence"})
ALLOWED_EVIDENCE_KEYS = frozenset({"path", "lines", "expect_sha256", "kind"})
EVIDENCE_KINDS = frozenset({"static", "execution"})
EVIDENCE_COUNT_KEYS = (
    "reference_ok_static",
    "reference_ok_execution",
    "missing",
    "stale",
    "digest_mismatch",
    "oversized",
)

_LINE_RANGE_RE = re.compile(r"^([1-9][0-9]{0,6})(?:-([1-9][0-9]{0,6}))?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_NOTES = (
    "deterministic index only: an existing file, a matched digest, or a "
    "recorded exit status is not demand coverage",
    "declared_kind is the author's declared provenance; a verified reference "
    "to a declared execution artifact is not a verified execution",
    "a root HEAD match does not prove the working-tree file bytes belong to "
    "that commit; per-reference digests are the file-level check",
    "a truncated snippet marks an incomplete selection, not a fully checked "
    "range",
    "self-reported claims carry no verified evidence",
)
_JEV_NOTES = (
    "a label applies only to the excerpts deliberately supplied for that "
    "claim; evidence omitted by the author limits the judgment",
    "a label reflects only the references that verified ok for that claim; "
    "its missing, stale, digest-mismatching, or truncated references were "
    "not evaluated",
    "confidence is not a measured accuracy; a label is advisory and never "
    "accepts, rejects, suppresses, or gates a delivery",
)

_INSTRUCTIONS_TEMPLATE = (
    "Advisory schema: "
    + QUESTION_VERSION
    + ". The state contains numbered claim blocks, each followed by its own "
    "Evidence excerpts. This question judges exactly one claim: the state "
    "block labeled \"Claim {position}\", quoted here: {claim_json}. Judge "
    "strictly from the evidence excerpts supplied in that same block "
    "whether the claim's substance is supported, contradicted, or "
    "insufficiently addressed. Excerpts that were not supplied count as "
    "insufficiently addressed, never as support, and a mere file-name or "
    "existence match is not coverage. Do not assume unquoted context and do "
    "not use any other claim's evidence, even when claim texts are "
    "identical."
)

_CRITERIA_TEXT = {
    "supported": (
        "The supplied excerpts, as quoted, directly establish the claim's "
        "substance."
    ),
    "contradicted": (
        "At least one supplied excerpt conflicts with the claim's substance."
    ),
    "insufficiently_addressed": (
        "The supplied excerpts neither establish nor contradict the claim: "
        "material parts are missing, only indirect, or merely nominal such "
        "as an existence check or exit status."
    ),
}


class HandoffEvidenceError(ValueError):
    """Operator-facing local input or configuration error."""


def load_handoff_input(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_file():
        raise HandoffEvidenceError(f"handoff input not found: {path}")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise HandoffEvidenceError("handoff input is not readable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise HandoffEvidenceError(
            f"handoff input exceeds {MAX_INPUT_BYTES} bytes: {len(raw)}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HandoffEvidenceError("handoff input is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise HandoffEvidenceError("handoff input must be a JSON object")
    return validate_handoff_payload(payload)


def validate_handoff_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an in-memory handoff payload with the closed schema.

    Applies exactly the checks of load_handoff_input after JSON decoding,
    so derived inputs (for example a parsed canonical draft) pass through
    the same validation surface as file inputs.
    """
    unknown = set(payload) - set(ALLOWED_TOP_KEYS)
    if unknown:
        raise HandoffEvidenceError(f"unknown handoff keys: {sorted(unknown)}")
    root_value = payload.get("root")
    if not isinstance(root_value, str) or not root_value.strip():
        raise HandoffEvidenceError("root must be a non-empty absolute directory path")
    root = Path(root_value)
    if not root.is_absolute():
        raise HandoffEvidenceError("root must be an absolute directory path")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise HandoffEvidenceError(f"root is not an existing directory: {root}") from exc
    if not resolved_root.is_dir():
        raise HandoffEvidenceError(f"root is not an existing directory: {root}")
    payload["root"] = str(resolved_root)
    if "handoff_id" in payload and (
        not isinstance(payload["handoff_id"], str) or not payload["handoff_id"].strip()
    ):
        raise HandoffEvidenceError("handoff_id must be a non-empty string when present")
    if "expect_commit" in payload and (
        not isinstance(payload["expect_commit"], str)
        or not re.fullmatch(r"[0-9a-fA-F]{7,40}", payload["expect_commit"])
    ):
        raise HandoffEvidenceError(
            "expect_commit must be a 7-40 hex character commit id when present"
        )
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        raise HandoffEvidenceError("claims must be a non-empty list")
    if len(claims) > MAX_CLAIMS:
        raise HandoffEvidenceError(
            f"too many claims for one bounded handoff: {len(claims)} > {MAX_CLAIMS}"
        )
    seen_ids: set[str] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise HandoffEvidenceError(f"claim {index} must be a JSON object")
        unknown = set(claim) - set(ALLOWED_CLAIM_KEYS)
        if unknown:
            raise HandoffEvidenceError(
                f"unknown keys in claim {index}: {sorted(unknown)}"
            )
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise HandoffEvidenceError(
                f"claim {index} field must be a non-empty string: claim_id"
            )
        if claim_id in seen_ids:
            raise HandoffEvidenceError(f"duplicate claim_id: {claim_id}")
        seen_ids.add(claim_id)
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HandoffEvidenceError(
                f"claim {claim_id} field must be a non-empty string: text"
            )
        evidence = claim.get("evidence", [])
        if not isinstance(evidence, list):
            raise HandoffEvidenceError(f"claim {claim_id} evidence must be a list")
        if len(evidence) > MAX_EVIDENCE_PER_CLAIM:
            raise HandoffEvidenceError(
                f"claim {claim_id} has too many evidence references: "
                f"{len(evidence)} > {MAX_EVIDENCE_PER_CLAIM}"
            )
        _validate_evidence_entries(claim_id, evidence, resolved_root)
    return payload


def _validate_evidence_entries(
    claim_id: str, evidence: list[Any], root: Path
) -> None:
    for position, entry in enumerate(evidence):
        if not isinstance(entry, dict):
            raise HandoffEvidenceError(
                f"claim {claim_id} evidence {position} must be a JSON object"
            )
        unknown = set(entry) - set(ALLOWED_EVIDENCE_KEYS)
        if unknown:
            raise HandoffEvidenceError(
                f"unknown keys in claim {claim_id} evidence {position}: "
                f"{sorted(unknown)}"
            )
        path_value = entry.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise HandoffEvidenceError(
                f"claim {claim_id} evidence {position} requires a path string"
            )
        candidate = Path(path_value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise HandoffEvidenceError(
                f"claim {claim_id} evidence {position} path must be relative "
                f"to the selected root without '..': {path_value}"
            )
        resolved = (root / candidate).resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise HandoffEvidenceError(
                f"claim {claim_id} evidence {position} escapes the selected "
                f"root: {path_value}"
            )
        if "lines" in entry:
            lines = entry["lines"]
            match = _LINE_RANGE_RE.match(lines) if isinstance(lines, str) else None
            if not match:
                raise HandoffEvidenceError(
                    f"claim {claim_id} evidence {position} lines must be 'N' "
                    f"or 'N-M' with N>=1: {lines!r}"
                )
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else start
            if end < start or end - start + 1 > MAX_LINE_RANGE_SPAN:
                raise HandoffEvidenceError(
                    f"claim {claim_id} evidence {position} line range span "
                    f"must be within 1..{MAX_LINE_RANGE_SPAN} lines"
                )
        if "expect_sha256" in entry:
            digest = entry["expect_sha256"]
            if not isinstance(digest, str) or not _SHA256_RE.match(digest):
                raise HandoffEvidenceError(
                    f"claim {claim_id} evidence {position} expect_sha256 must "
                    "be a 64 hex character digest"
                )
        if "kind" in entry and entry["kind"] not in EVIDENCE_KINDS:
            raise HandoffEvidenceError(
                f"claim {claim_id} evidence {position} kind must be one of "
                f"{', '.join(sorted(EVIDENCE_KINDS))}"
            )


def _root_commit_status(root: Path, expected: str) -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"expected": expected, "observed": "", "status": "unavailable"}
    if completed.returncode != 0:
        return {"expected": expected, "observed": "", "status": "unavailable"}
    observed = completed.stdout.strip().lower()
    status = "match" if observed.startswith(expected.lower()) else "mismatch"
    return {"expected": expected, "observed": observed, "status": status}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _decode_snippet(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _bounded_snippet(raw: bytes) -> tuple[str | None, bool]:
    """Cut at the byte cap, keep a valid UTF-8 prefix, label truncation.

    Returns (snippet, truncated). A cut never splits a multi-byte character
    silently: at most one incomplete trailing sequence is dropped and the
    snippet is then marked truncated. Content that is not UTF-8 beyond such
    a trailing sequence returns None (binary).
    """
    cut = len(raw) > MAX_SNIPPET_BYTES
    if cut:
        raw = raw[:MAX_SNIPPET_BYTES]
    snippet = _decode_snippet(raw)
    if snippet is not None:
        return snippet, cut
    for drop in (1, 2, 3):
        if drop > len(raw):
            break
        prefix = _decode_snippet(raw[:-drop])
        if prefix is not None:
            return prefix, True
    return None, cut


def _line_range(value: str) -> tuple[int, int]:
    match = _LINE_RANGE_RE.match(value)
    if match is None:
        raise HandoffEvidenceError(f"invalid line range: {value!r}")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    return start, end


def _verify_evidence_entry(root: Path, entry: Mapping[str, Any]) -> dict[str, Any]:
    relative = str(entry["path"])
    path = (root / relative).resolve()
    record: dict[str, Any] = {
        "path": relative,
        "declared_kind": str(entry.get("kind", "static")),
    }
    if "lines" in entry:
        record["lines"] = str(entry["lines"])
    if not path.exists() or not path.is_file():
        record["status"] = "missing"
        return record
    try:
        size = path.stat().st_size
    except OSError:
        record["status"] = "missing"
        return record
    if size > MAX_EVIDENCE_FILE_BYTES:
        record["status"] = "oversized"
        return record
    if "expect_sha256" in entry:
        actual = _file_sha256(path)
        record["sha256"] = actual
        if actual != str(entry["expect_sha256"]).lower():
            record["status"] = "digest_mismatch"
            return record
    elif "lines" not in entry:
        record["sha256"] = _file_sha256(path)
    if "lines" in entry:
        try:
            raw = path.read_bytes()
        except OSError:
            record["status"] = "missing"
            return record
        file_lines = raw.splitlines()
        start, end = _line_range(str(entry["lines"]))
        if end > len(file_lines):
            record["status"] = "stale"
            record["observed_line_count"] = len(file_lines)
            return record
        snippet, truncated = _bounded_snippet(b"\n".join(file_lines[start - 1 : end]))
        record["status"] = "ok"
        record["snippet"] = snippet
        record["truncated"] = truncated
        record["attribution"] = (
            f"{relative}:{start}-{end}" if end != start else f"{relative}:{start}"
        )
        return record
    with path.open("rb") as handle:
        snippet, truncated = _bounded_snippet(handle.read(MAX_SNIPPET_BYTES + 1))
    record["status"] = "ok"
    record["snippet"] = snippet
    record["truncated"] = truncated
    record["attribution"] = relative
    return record


def build_offline_index(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(str(payload["root"]))
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "mode": "offline",
        "root": str(root),
        "notes": list(_NOTES),
        "claims": [],
    }
    if payload.get("handoff_id") is not None:
        result["handoff_id"] = str(payload["handoff_id"])
    if payload.get("expect_commit"):
        result["root_commit"] = _root_commit_status(
            root, str(payload["expect_commit"])
        )
    for claim in payload["claims"]:
        evidence_records = [
            _verify_evidence_entry(root, entry) for entry in claim.get("evidence", [])
        ]
        counts = dict.fromkeys(EVIDENCE_COUNT_KEYS, 0)
        for record in evidence_records:
            if record["status"] == "ok":
                key = (
                    "reference_ok_execution"
                    if record["declared_kind"] == "execution"
                    else "reference_ok_static"
                )
            else:
                key = record["status"]
            counts[key] += 1
        result["claims"].append(
            {
                "claim_id": str(claim["claim_id"]),
                "text": str(claim["text"]),
                "self_reported": not evidence_records,
                "evidence_counts": counts,
                "evidence": evidence_records,
            }
        )
    return result


def _usable_evidence(claim_record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        entry
        for entry in claim_record["evidence"]
        if entry["status"] == "ok" and entry.get("snippet")
    ]


def build_jev_state(
    index: Mapping[str, Any], texts: Mapping[str, str]
) -> tuple[str, list[dict[str, str]]]:
    blocks: list[str] = []
    asked: list[dict[str, str]] = []
    for claim_record in index["claims"]:
        usable = _usable_evidence(claim_record)
        if not usable:
            continue
        excerpts = []
        for entry in usable:
            excerpt = str(entry["snippet"])
            if entry.get("truncated"):
                excerpt += "\n[excerpt truncated]"
            excerpts.append(excerpt)
        position = len(asked) + 1
        blocks.append(
            f"Claim {position}:\n{texts[str(claim_record['claim_id'])]}\n\n"
            f"Evidence:\n" + "\n\n".join(excerpts)
        )
        asked.append({"claim_id": str(claim_record["claim_id"]), "position": position})
    if not blocks:
        return "", []
    state = "\n\n---\n\n".join(blocks)
    encoded = state.encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise HandoffEvidenceError(
            f"jev state exceeds {MAX_STATE_BYTES} bytes: {len(encoded)}; "
            "shrink claims or snippets"
        )
    return state, asked


def build_jev_request(
    state: str, asked: list[dict[str, str]], texts: Mapping[str, str]
) -> dict[str, Any]:
    questions: dict[str, Any] = {}
    for item in asked:
        instructions = _INSTRUCTIONS_TEMPLATE.format(
            position=item["position"],
            claim_json=json.dumps(texts[item["claim_id"]], ensure_ascii=False),
        )
        questions[f"claim{item['position']}"] = {
            "type": "choice",
            "instructions": instructions,
            "criteria": {choice: _CRITERIA_TEXT[choice] for choice in SUPPORT_CHOICES},
        }
    return {"state": state, "model": JEV_MODEL, "questions": questions}


def parse_jev_response(raw: bytes, asked: list[dict[str, str]]) -> dict[str, Any]:
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
    if not isinstance(answers, dict):
        raise JevShadowUnavailable("malformed_response")
    labels: dict[str, dict[str, Any]] = {}
    for item in asked:
        question_id = f"claim{item['position']}"
        answer = answers.get(question_id)
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise JevShadowUnavailable("malformed_response")
        choice = answer.get("choice")
        if choice not in SUPPORT_CHOICES:
            raise JevShadowUnavailable("malformed_response")
        probabilities_raw = answer.get("probabilities")
        if not isinstance(probabilities_raw, dict):
            raise JevShadowUnavailable("malformed_response")
        if set(probabilities_raw) != set(SUPPORT_CHOICES):
            raise JevShadowUnavailable("malformed_response")
        for value in probabilities_raw.values():
            if not _unit_interval_number(value):
                raise JevShadowUnavailable("malformed_response")
        if abs(sum(probabilities_raw.values()) - 1) > PROBABILITY_SUM_TOLERANCE:
            raise JevShadowUnavailable("malformed_response")
        if probabilities_raw[choice] != max(probabilities_raw.values()):
            raise JevShadowUnavailable("malformed_response")
        confidence = answer.get("confidence")
        if not _unit_interval_number(confidence):
            raise JevShadowUnavailable("malformed_response")
        labels[str(item["claim_id"])] = {
            "choice": choice,
            "probabilities": dict(probabilities_raw),
            "confidence": confidence,
        }
    usage_raw = payload.get("usage")
    if not isinstance(usage_raw, dict):
        raise JevShadowUnavailable("malformed_response")
    if not _nonneg_int_tokens(usage_raw.get("input_tokens")) or not _nonneg_int_tokens(
        usage_raw.get("output_tokens")
    ):
        raise JevShadowUnavailable("malformed_response")
    return {
        "model_reported": model,
        "labels": labels,
        "usage": {
            "input_tokens": usage_raw["input_tokens"],
            "output_tokens": usage_raw["output_tokens"],
        },
    }


def _validate_timeout(timeout_seconds: float) -> None:
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise HandoffEvidenceError("timeout must be a finite number")
    if not math.isfinite(timeout_seconds) or not (
        0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise HandoffEvidenceError(
            f"timeout must be finite and within (0, {MAX_TIMEOUT_SECONDS}] seconds"
        )


def execute_handoff(
    payload: Mapping[str, Any],
    *,
    jev_live: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport=None,
    api_key: str | None = None,
) -> dict[str, Any]:
    _validate_timeout(timeout_seconds)
    index = build_offline_index(payload)
    if not jev_live:
        return index
    texts = {
        str(claim["claim_id"]): str(claim["text"]) for claim in payload["claims"]
    }
    commit = index.get("root_commit")
    if isinstance(commit, dict) and commit.get("status") == "mismatch":
        index["jev"] = {
            "mode": "live",
            "question_version": QUESTION_VERSION,
            "model_requested": JEV_MODEL,
            "endpoint": ENDPOINT,
            "asked_claims": [],
            "not_asked": [str(record["claim_id"]) for record in index["claims"]],
            "notes": list(_JEV_NOTES),
            "status": "not_asked_root_version_mismatch",
        }
        return index
    state, asked = build_jev_state(index, texts)
    jev: dict[str, Any] = {
        "mode": "live",
        "question_version": QUESTION_VERSION,
        "model_requested": JEV_MODEL,
        "endpoint": ENDPOINT,
        "asked_claims": [item["claim_id"] for item in asked],
        "not_asked": [
            str(record["claim_id"])
            for record in index["claims"]
            if not _usable_evidence(record)
        ],
        "notes": list(_JEV_NOTES),
    }
    if not asked:
        jev["status"] = "not_asked_no_verified_evidence"
        index["jev"] = jev
        return index
    request = build_jev_request(state, asked, texts)
    request_bytes = json.dumps(request, ensure_ascii=False).encode("utf-8")
    jev["request_sha256"] = "sha256:" + hashlib.sha256(request_bytes).hexdigest()
    effective_transport = transport or urllib_transport
    effective_key = (
        api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
    )
    started = time.monotonic()
    try:
        raw = effective_transport(request_bytes, effective_key, timeout_seconds)
        parsed = parse_jev_response(raw, asked)
        if parsed["model_reported"] != JEV_MODEL:
            jev["model_reported"] = parsed["model_reported"]
            raise JevShadowUnavailable("model_mismatch")
    except JevShadowUnavailable as exc:
        jev["status"] = "unavailable"
        jev["unavailable_reason"] = exc.reason
        jev["duration_ms"] = max(1, round((time.monotonic() - started) * 1000))
        index["mode"] = "jev-unavailable"
        index["jev"] = jev
        return index
    jev["status"] = "advised"
    jev["duration_ms"] = max(1, round((time.monotonic() - started) * 1000))
    jev["model_reported"] = parsed["model_reported"]
    jev["usage"] = parsed["usage"]
    jev["labels"] = parsed["labels"]
    index["mode"] = "jev-live"
    index["jev"] = jev
    return index


def _reference_fix_line(entry: Mapping[str, Any]) -> str | None:
    """One concise, actionable repair hint for one evidence reference.

    Says what is wrong — including the requested range and the observed
    size where the check recorded them — plus the narrowest concrete
    correction a worker can apply locally. Rendered before any excerpt so
    the hint is readable without scrolling past a long snippet. Returns
    ``None`` for a fully verified, untruncated reference: a clean ``ok``
    needs no repair and is never worded as a semantic pass.
    """

    status = str(entry.get("status") or "")
    relative = str(entry.get("path") or "")
    if status == "ok":
        if not entry.get("truncated"):
            return None
        return (
            f"truncated excerpt: only the first {MAX_SNIPPET_BYTES} bytes of "
            "this range are shown, so the selection is incomplete "
            "evidence, not proof the claim is false; narrow the reference "
            "to the decisive lines — for a command or test log, the "
            "summary and exit lines such as the `Ran`/`OK`/`FAILED` and "
            "exit-status tail — then submit the draft again"
        )
    if status == "stale":
        requested = str(entry["lines"])
        observed = int(entry["observed_line_count"])
        start, _end = _line_range(requested)
        if start > observed:
            return (
                f"stale range: requested lines {requested} but the file now "
                f"has {observed} line(s), so the requested start is already "
                "past the end; point the reference at lines that exist or "
                "drop the reference"
            )
        return (
            f"stale range: requested lines {requested} but the file now has "
            f"{observed} lines; fix the reference to end within the file "
            f"(for example `{relative}:{start}-{observed}`), re-check the "
            "current content, or narrow the claim"
        )
    if status == "missing":
        return (
            f"missing file: nothing exists at `{relative}` under the root; "
            "correct the path or produce the artifact before relying on "
            "this claim"
        )
    if status == "digest_mismatch":
        current = str(entry.get("sha256") or "a different digest")
        return (
            "digest mismatch: the current file bytes hash to "
            f"`{current}`; re-check the current content and refresh "
            "`expect_sha256` only if those bytes still support the claim"
        )
    if status == "oversized":
        return (
            f"oversized file: `{relative}` exceeds the "
            f"{MAX_EVIDENCE_FILE_BYTES // (1024 * 1024)} MiB evidence cap "
            "at the whole-file level, and adding or changing a `lines` "
            "range on this same reference does not lift that cap; cite a "
            "slimmer evidence file that is already under the cap (with "
            "its own source attribution), or narrow the claim / "
            "truthfully disclose the unresolved evidence"
        )
    return None


def render_markdown(index: Mapping[str, Any]) -> str:
    lines = ["## Handoff evidence index", ""]
    header = f"- mode: {index['mode']}"
    if index.get("handoff_id") is not None:
        header += f" (handoff `{index['handoff_id']}`)"
    lines.append(header)
    commit = index.get("root_commit")
    if isinstance(commit, dict):
        lines.append(
            f"- root commit: {commit.get('status')}"
            f" (expected {commit.get('expected')},"
            f" observed {commit.get('observed') or 'n/a'};"
            " HEAD match does not verify working-tree bytes)"
        )
    lines.append("")
    lines.append(
        "Limits: excerpts and labels cover only the references supplied and "
        "verified `ok`; omitted, missing, stale, or truncated references "
        "were not evaluated. `declared execution` is author-declared "
        "provenance, not an independently verified execution. Jev labels "
        "are advisory: confidence is not measured accuracy and nothing here "
        "accepts or rejects the delivery. An `ok` reference verifies only "
        "the quoted excerpt for that reference; it is never a semantic "
        "pass over the claim."
    )
    lines.append("")
    jev = index.get("jev") if isinstance(index.get("jev"), dict) else None
    labels = jev.get("labels", {}) if jev else {}
    for record in index["claims"]:
        claim_id = str(record["claim_id"])
        parts = [f"- claim `{claim_id}`: {record['text']}"]
        label = labels.get(claim_id)
        if label:
            parts.append(
                f"**{label['choice']}** (jev, confidence {label['confidence']})"
            )
        if record["self_reported"]:
            parts.append("self-reported, no evidence references supplied")
        lines.append(" — ".join(parts))
        for entry in record["evidence"]:
            head = (
                f"  - {entry.get('attribution') or entry['path']}"
                f" (declared {entry['declared_kind']}, {entry['status']}"
            )
            if entry.get("truncated"):
                head += ", truncated"
            if entry["status"] == "ok" and entry.get("snippet") is None:
                head += ", binary"
            head += ")"
            lines.append(head)
            fix = _reference_fix_line(entry)
            if fix:
                lines.append(f"    ! {fix}")
            snippet = entry.get("snippet")
            if snippet:
                lines.extend(
                    f"    | {snippet_line}" for snippet_line in snippet.splitlines()
                )
    if jev and jev.get("status") == "unavailable":
        lines.append("")
        lines.append(
            f"- jev unavailable (`{jev.get('unavailable_reason')}`); "
            "offline index above is unchanged"
        )
    if jev and jev.get("status", "").startswith("not_asked"):
        lines.append("")
        lines.append(f"- jev not asked (`{jev.get('status')}`)")
    lines.append("")
    return "\n".join(lines)
