"""Upgrade evidence receipts for runtime candidate bundles.

Two artifact types with different lifecycles, kept deliberately separate:

1. An immutable candidate bundle manifest recording one actually tested
   OpenCode/OMO/profile/adapter combination, the control-plane revision, the
   config digest, and the normalized digest of the API contract that was
   actually consumed. Manifests are content addressed; rewriting an existing
   manifest with different content is refused.

2. An append-only observation receipt ledger recording one shadow/canary
   observation per run: candidate bundle digest, scope, frozen identity
   snapshot, compared fields, consistency result, and artifact references.
   Exact duplicate receipts (identical receipt digest) are skipped
   idempotently; any content difference is a new observation.

This module owns no submission, activation, promotion, or version-matrix
behavior. The documented qualification chain is ``candidate ->
shadow_passed -> canary_passed -> approved``, but this slice provides only
the candidate data seam: the builder creates ``candidate`` manifests only,
and promotion is left to a future evidence-gated API. ``active`` is not a
qualification state and must be proven separately by explicit install/sync,
a fresh process, and a runtime probe.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


CANDIDATE_BUNDLE_MANIFEST_KIND = "runtime_candidate_bundle_manifest"
OBSERVATION_RECEIPT_KIND = "runtime_upgrade_observation_receipt"
UPGRADE_RECEIPT_SCHEMA_VERSION = 1
QUALIFICATION_STATES = (
    "candidate",
    "shadow_passed",
    "canary_passed",
    "approved",
)
OBSERVATION_SCOPES = ("unbound_capability", "bound_shadow")

_SHA256_HEX_LENGTH = 64
_COMMIT_HEX_LENGTH = 40


class UpgradeReceiptError(Exception):
    """Raised when an upgrade receipt or manifest violates its contract."""


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_hex(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpgradeReceiptError(f"{field} must be a non-empty string")
    return value


def _require_text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise UpgradeReceiptError(f"{field} must be a list of non-empty strings")
    return sorted(set(value))


def _require_evidence_list(value: Any, field: str) -> list[str]:
    normalized = _require_text_list(value, field)
    if not normalized:
        raise UpgradeReceiptError(f"{field} must be a non-empty list")
    if normalized != value:
        raise UpgradeReceiptError(f"{field} must be sorted and deduplicated")
    return normalized


def candidate_bundle_digest(manifest: dict[str, Any]) -> str:
    """Digest of a manifest, excluding any embedded digest field."""

    payload = {key: value for key, value in manifest.items() if key != "bundle_digest"}
    return _sha256_hex(payload)


def _normalize_consumed_contract(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UpgradeReceiptError("consumed_api_contract must be an object")
    endpoints = raw.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise UpgradeReceiptError("consumed_api_contract.endpoints must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, dict):
            raise UpgradeReceiptError(f"consumed_api_contract.endpoints[{index}] must be an object")
        method = _require_text(endpoint.get("method"), f"endpoints[{index}].method").upper()
        path = _require_text(endpoint.get("path"), f"endpoints[{index}].path")
        key = (method, path)
        if key in seen:
            raise UpgradeReceiptError(f"duplicate consumed endpoint {method} {path}")
        seen.add(key)
        normalized.append(
            {
                "method": method,
                "path": path,
                "request_fields": _require_text_list(
                    endpoint.get("request_fields"), f"endpoints[{index}].request_fields"
                ),
                "response_fields": _require_text_list(
                    endpoint.get("response_fields"), f"endpoints[{index}].response_fields"
                ),
            }
        )
    normalized.sort(key=lambda item: (item["method"], item["path"]))
    return {
        "endpoints": normalized,
        "contract_digest": _sha256_hex({"endpoints": normalized}),
    }


def _normalize_runtime(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UpgradeReceiptError("runtime must be an object")
    return {
        "type": _require_text(raw.get("type"), "runtime.type"),
        "version": _require_text(raw.get("version"), "runtime.version"),
    }


def _normalize_adapters(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise UpgradeReceiptError("adapters must be a non-empty list")
    normalized = []
    for index, adapter in enumerate(raw):
        if not isinstance(adapter, dict):
            raise UpgradeReceiptError(f"adapters[{index}] must be an object")
        normalized.append(
            {
                "name": _require_text(adapter.get("name"), f"adapters[{index}].name"),
                "version": _require_text(adapter.get("version"), f"adapters[{index}].version"),
            }
        )
    return sorted(normalized, key=lambda item: item["name"])


def _normalize_omo(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UpgradeReceiptError("omo must be an object")
    present = raw.get("present")
    if type(present) is not bool:
        raise UpgradeReceiptError("omo.present must be boolean")
    version = raw.get("version") or ""
    if present:
        _require_text(version, "omo.version")
    elif version:
        raise UpgradeReceiptError("omo.version must be empty when omo.present is false")
    return {"present": present, "version": version if present else ""}


def build_candidate_bundle_manifest(
    *,
    runtime: dict[str, Any],
    adapters: list[dict[str, Any]],
    omo: dict[str, Any],
    profile: str,
    control_plane_revision: str,
    config_digest: str,
    openapi_schema_digest: str,
    consumed_api_contract: dict[str, Any],
    probed_at: str,
    status: str = "candidate",
) -> dict[str, Any]:
    """Build one immutable candidate bundle manifest.

    This slice provides only the candidate data seam: ``status`` must be
    ``"candidate"``. Promotion to ``shadow_passed``/``canary_passed``/
    ``approved`` requires a future evidence-gated API; ``active`` is never
    a qualification state.
    """

    if status != "candidate":
        raise UpgradeReceiptError(
            "builder only creates candidate bundles; promotion to "
            f"{QUALIFICATION_STATES[1:]} requires an evidence-gated API, and "
            "'active' is not a qualification state"
        )
    revision = _require_text(control_plane_revision, "control_plane_revision")
    if len(revision) != _COMMIT_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in revision
    ):
        raise UpgradeReceiptError("control_plane_revision must be 40 lowercase hex")
    if not _is_sha256(config_digest):
        raise UpgradeReceiptError("config_digest must be a sha256 hex digest")
    if not _is_sha256(openapi_schema_digest):
        raise UpgradeReceiptError("openapi_schema_digest must be a sha256 hex digest")
    manifest = {
        "manifest_kind": CANDIDATE_BUNDLE_MANIFEST_KIND,
        "schema_version": UPGRADE_RECEIPT_SCHEMA_VERSION,
        "runtime": _normalize_runtime(runtime),
        "adapters": _normalize_adapters(adapters),
        "omo": _normalize_omo(omo),
        "profile": _require_text(profile, "profile"),
        "control_plane_revision": revision,
        "config_digest": config_digest,
        "openapi_schema_digest": openapi_schema_digest,
        "consumed_api_contract": _normalize_consumed_contract(consumed_api_contract),
        "probed_at": _require_text(probed_at, "probed_at"),
        "status": status,
    }
    manifest["bundle_digest"] = candidate_bundle_digest(manifest)
    return manifest


_CANDIDATE_BUNDLE_MANIFEST_FIELDS = frozenset(
    (
        "manifest_kind",
        "schema_version",
        "runtime",
        "adapters",
        "omo",
        "profile",
        "control_plane_revision",
        "config_digest",
        "openapi_schema_digest",
        "consumed_api_contract",
        "probed_at",
        "status",
        "bundle_digest",
    )
)


def validate_candidate_bundle_manifest(manifest: Any) -> dict[str, Any]:
    """Fully validate one candidate bundle manifest at a trust boundary.

    Rejects self-consistent but illegal manifests: wrong kind or schema,
    missing/extra fields, ``active`` or unknown qualification status,
    non-canonical sections, malformed digests, and stale bundle digests.
    Contract validation runs before digest verification.
    """

    if not isinstance(manifest, dict):
        raise UpgradeReceiptError("manifest must be an object")
    fields = set(manifest)
    if fields != _CANDIDATE_BUNDLE_MANIFEST_FIELDS:
        missing = sorted(_CANDIDATE_BUNDLE_MANIFEST_FIELDS - fields)
        extra = sorted(fields - _CANDIDATE_BUNDLE_MANIFEST_FIELDS)
        raise UpgradeReceiptError(
            f"manifest fields mismatch: missing={missing} extra={extra}"
        )
    if manifest["manifest_kind"] != CANDIDATE_BUNDLE_MANIFEST_KIND:
        raise UpgradeReceiptError("manifest_kind mismatch")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != UPGRADE_RECEIPT_SCHEMA_VERSION
    ):
        raise UpgradeReceiptError("schema_version mismatch")
    if manifest["status"] != "candidate":
        raise UpgradeReceiptError(
            "status must be 'candidate' in this slice; shadow_passed, "
            "canary_passed, and approved require a future evidence-gated "
            "promotion API, and 'active' is not a qualification state"
        )
    if manifest["runtime"] != _normalize_runtime(manifest["runtime"]):
        raise UpgradeReceiptError("runtime is not in canonical form")
    if manifest["adapters"] != _normalize_adapters(manifest["adapters"]):
        raise UpgradeReceiptError("adapters are not in canonical form")
    if manifest["omo"] != _normalize_omo(manifest["omo"]):
        raise UpgradeReceiptError("omo is not in canonical form")
    if manifest["consumed_api_contract"] != _normalize_consumed_contract(
        manifest["consumed_api_contract"]
    ):
        raise UpgradeReceiptError("consumed_api_contract is not in canonical form")
    _require_text(manifest["profile"], "profile")
    revision = _require_text(manifest["control_plane_revision"], "control_plane_revision")
    if len(revision) != _COMMIT_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in revision
    ):
        raise UpgradeReceiptError("control_plane_revision must be 40 lowercase hex")
    if not _is_sha256(manifest["config_digest"]):
        raise UpgradeReceiptError("config_digest must be a sha256 hex digest")
    if not _is_sha256(manifest["openapi_schema_digest"]):
        raise UpgradeReceiptError("openapi_schema_digest must be a sha256 hex digest")
    _require_text(manifest["probed_at"], "probed_at")
    digest = manifest["bundle_digest"]
    if not _is_sha256(digest) or digest != candidate_bundle_digest(manifest):
        raise UpgradeReceiptError("bundle_digest does not match manifest content")
    return manifest


def write_candidate_bundle_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    """Write a manifest content-addressed by bundle digest.

    The full manifest contract is re-validated before writing; a
    self-consistent digest never substitutes for contract validation.
    Rewriting the same content is idempotent; rewriting with different
    content under the same digest fails closed.
    """

    manifest = validate_candidate_bundle_manifest(manifest)
    digest = manifest["bundle_digest"]
    directory = Path(directory)
    path = directory / f"candidate-bundle-{digest}.json"
    serialized = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpgradeReceiptError(
                f"existing candidate bundle manifest is unreadable: {path.name}"
            ) from exc
        if existing != manifest:
            raise UpgradeReceiptError("candidate bundle manifests are immutable")
        return path
    directory.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


PROMPT_ASYNC_CONSUMED_PATH = "/session/{sessionID}/prompt_async"
PROMPT_ASYNC_CONSUMED_PROJECTION = {
    "method": "POST",
    "path": PROMPT_ASYNC_CONSUMED_PATH,
    "content_type": "application/json",
    "request_schema_type": "object",
    "required_fields": ["parts"],
    "parts_type": "array",
    "parts_item_admits_text_part": {
        "schema_type": "object",
        "allowed_required_fields": ["text", "type"],
        "discriminator": {"property": "type", "permits": "text"},
        "text_property_type": "string",
    },
    "response_status": "204",
    "supported_subset": {
        "root_allowed_keys": [
            "additionalProperties",
            "description",
            "properties",
            "required",
            "title",
            "type",
        ],
        "parts_allowed_keys": ["description", "items", "title", "type"],
        "items_anyof_allowed_keys": ["anyOf", "description", "title"],
        "part_candidate_allowed_keys": [
            "additionalProperties",
            "description",
            "properties",
            "required",
            "title",
            "type",
        ],
        "discriminator_allowed_keys": [
            "const",
            "description",
            "enum",
            "title",
            "type",
        ],
        "text_property_allowed_keys": ["description", "title", "type"],
        "text_property_unrestricted": True,
        "assertion_values": {
            "discriminator_enum": (
                "if the enum key is present it must be a list of strings "
                "containing text; a present null enum is rejected"
            ),
            "discriminator_const": (
                "if the const key is present it must equal text; a present "
                "null const is rejected"
            ),
            "additional_properties": (
                "if present at the root or text candidate it must be a "
                "boolean or mapping; null, numbers, strings and lists are "
                "rejected; mapping values are not recursively inspected "
                "because every emitted field is declared"
            ),
            "response_204": (
                "the declared 204 response entry must resolve through local "
                "refs to a mapping without an unresolved $ref; response "
                "body and description details are unconsumed"
            ),
        },
    },
}

SESSION_CREATE_CONSUMED_PATH = "/session"
SESSION_CREATE_CONSUMED_PROJECTION = {
    "method": "POST",
    "path": SESSION_CREATE_CONSUMED_PATH,
    "content_type": "application/json",
    "request_schema_type": "object",
    "required_fields": [],
    "emitted_fields": ["permission", "title"],
    "title_property_type": "string",
    "permission_property_type": "array",
    "permission_item_fields": ["action", "pattern", "permission"],
    "permission_actions": ["allow", "deny"],
    "response_status": "200",
    "response_schema_type": "object",
    "consumed_response_fields": {
        "directory": "string",
        "id": "string",
        "title": "string",
    },
    "id_pattern": "^ses",
}
TUI_SELECT_SESSION_CONSUMED_PATH = "/tui/select-session"
TUI_SELECT_SESSION_CONSUMED_PROJECTION = {
    "method": "POST",
    "path": TUI_SELECT_SESSION_CONSUMED_PATH,
    "content_type": "application/json",
    "request_schema_type": "object",
    "required_fields": ["sessionID"],
    "emitted_fields": ["sessionID"],
    "session_id_property_type": "string",
    "session_id_pattern": "^ses",
    "response_status": "200",
    "response_schema_type": "boolean",
}

PROMPT_ASYNC_SUPPLEMENT_CONSUMED_PROJECTION = {
    **PROMPT_ASYNC_CONSUMED_PROJECTION,
    "emitted_optional_fields": ["agent", "model", "variant"],
    "selection_field_schemas": {
        "agent": {"type": "string"},
        "variant": {"type": "string"},
        "model": {
            "type": "object",
            "required": ["providerID", "modelID"],
            "property_types": {"providerID": "string", "modelID": "string"},
        },
    },
    "selection_defaults": (
        "an omitted agent or model is resolved by the backend before the "
        "session current choice, so a supplement must emit the bound agent, "
        "model, and variant explicitly; an empty bound variant is emitted as "
        "the backend's canonical 'default' sentinel"
    ),
    "session_selection_metadata": (
        "GET /session/{sessionID} carries optional agent and "
        "model{providerID,modelID,variant}; the supplement verifies these "
        "bounded fields against the bound selection before its one POST"
    ),
}


def prompt_async_supplement_consumed_schema_digest() -> str:
    """Canonical digest of the selection-capable supplement prompt_async contract."""

    return _sha256_hex(PROMPT_ASYNC_SUPPLEMENT_CONSUMED_PROJECTION)


def prompt_async_consumed_schema_digest() -> str:
    """Canonical digest of the consumed POST prompt_async interface."""

    return _sha256_hex(PROMPT_ASYNC_CONSUMED_PROJECTION)


SESSION_ABORT_CONSUMED_PATH = "/session/{sessionID}/abort"
SESSION_ABORT_CONSUMED_PROJECTION = {
    "method": "POST",
    "path": SESSION_ABORT_CONSUMED_PATH,
    "request_body": "none",
    "response_status": "200",
    "response_schema_type": "boolean",
    "semantics": (
        "the pinned backend interrupts the session's tracked run and cancels "
        "its background jobs; the boolean response is the measured cancel "
        "result, not proof that every external tool, plugin, MCP or remote "
        "writer has stopped"
    ),
}


def cold_start_consumed_schema_digest() -> str:
    """Canonical digest of create-session followed by prompt_async."""

    return _sha256_hex(
        {
            "prompt_async": PROMPT_ASYNC_CONSUMED_PROJECTION,
            "session_create": SESSION_CREATE_CONSUMED_PROJECTION,
            "tui_select_session": TUI_SELECT_SESSION_CONSUMED_PROJECTION,
        }
    )


def resolve_pinned_candidate_bundle_manifest(
    path: str,
    *,
    expected_bundle_digest: str,
) -> dict[str, Any]:
    """Resolve and fully validate one pinned candidate bundle manifest.

    The path must be an absolute path to a canonical regular file, the
    content must pass the full manifest contract, and the recomputed
    bundle digest must equal ``expected_bundle_digest``. Used at grant
    preparation and again at submission so a swapped or tampered manifest
    fails closed.
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        raise UpgradeReceiptError(
            "candidate manifest path must be absolute"
        )
    if candidate.is_symlink() or not candidate.is_file():
        raise UpgradeReceiptError(
            f"candidate manifest is not a canonical regular file: {path}"
        )
    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpgradeReceiptError(
            f"candidate manifest is unreadable: {path}"
        ) from exc
    manifest = validate_candidate_bundle_manifest(raw)
    if not _is_sha256(expected_bundle_digest):
        raise UpgradeReceiptError(
            "expected_bundle_digest must be a sha256 hex digest"
        )
    if manifest["bundle_digest"] != expected_bundle_digest:
        raise UpgradeReceiptError(
            "candidate manifest bundle digest mismatch"
        )
    return manifest


def require_consumed_server_prompt_async_contract(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Require the consumed API contract to cover POST prompt_async.

    Returns the matching endpoint entry. The consumed contract must
    declare exactly the POST ``/session/{sessionID}/prompt_async`` path
    with the ``parts`` request field; anything less fails closed before
    grant append or submission. The selection-capable supplement projection
    is accepted because it is a strict superset of the parts-only payload
    requirements.
    """

    if manifest.get("openapi_schema_digest") not in {
        prompt_async_consumed_schema_digest(),
        prompt_async_supplement_consumed_schema_digest(),
    }:
        raise UpgradeReceiptError(
            "candidate manifest openapi_schema_digest does not match the "
            "canonical consumed prompt_async projection digest"
        )
    contract = manifest.get("consumed_api_contract")
    endpoints = contract.get("endpoints") if isinstance(contract, dict) else None
    if not isinstance(endpoints, list):
        raise UpgradeReceiptError(
            f"consumed_api_contract lacks POST {PROMPT_ASYNC_CONSUMED_PATH}"
        )
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        if (
            endpoint.get("method") == "POST"
            and endpoint.get("path") == PROMPT_ASYNC_CONSUMED_PATH
        ):
            request_fields = endpoint.get("request_fields")
            response_fields = endpoint.get("response_fields")
            if request_fields != ["parts"] or response_fields != []:
                raise UpgradeReceiptError(
                    f"consumed POST {PROMPT_ASYNC_CONSUMED_PATH} must declare "
                    "exactly request_fields=['parts'] and response_fields=[]"
                )
            return endpoint
    raise UpgradeReceiptError(
        f"consumed_api_contract lacks POST {PROMPT_ASYNC_CONSUMED_PATH}"
    )


def require_consumed_server_supplement_contract(
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Require the consumed API contract to cover a selection-capable supplement.

    A parts-only consumed receipt cannot prove that the original
    agent/model/variant travel with a same-task supplement, so it is refused
    here; a manifest whose digest is the supplement projection is required.
    """
    if manifest.get("openapi_schema_digest") != (
        prompt_async_supplement_consumed_schema_digest()
    ):
        raise UpgradeReceiptError(
            "candidate manifest openapi_schema_digest does not match the "
            "canonical selection-capable supplement prompt_async projection "
            "digest"
        )
    endpoint = require_consumed_server_prompt_async_contract(manifest)
    return {
        "endpoint": endpoint,
        "emitted_optional_fields": list(
            PROMPT_ASYNC_SUPPLEMENT_CONSUMED_PROJECTION[
                "emitted_optional_fields"
            ]
        ),
    }


def require_consumed_server_cold_start_contract(
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Require the exact create-session and prompt_async consumed interfaces."""

    if manifest.get("openapi_schema_digest") != cold_start_consumed_schema_digest():
        raise UpgradeReceiptError(
            "candidate manifest openapi_schema_digest does not match the "
            "canonical consumed cold-start projection digest"
        )
    contract = manifest.get("consumed_api_contract")
    endpoints = contract.get("endpoints") if isinstance(contract, dict) else None
    if not isinstance(endpoints, list):
        raise UpgradeReceiptError(
            f"consumed_api_contract lacks POST {SESSION_CREATE_CONSUMED_PATH}"
        )
    create_endpoint = next(
        (
            endpoint
            for endpoint in endpoints
            if isinstance(endpoint, dict)
            and endpoint.get("method") == "POST"
            and endpoint.get("path") == SESSION_CREATE_CONSUMED_PATH
        ),
        None,
    )
    if create_endpoint is None:
        raise UpgradeReceiptError(
            f"consumed_api_contract lacks POST {SESSION_CREATE_CONSUMED_PATH}"
        )
    if (
        create_endpoint.get("request_fields") != ["permission", "title"]
        or create_endpoint.get("response_fields") != ["directory", "id", "title"]
    ):
        raise UpgradeReceiptError(
            "consumed POST /session must declare exactly "
            "request_fields=['permission', 'title'] and "
            "response_fields=['directory', 'id', 'title']"
        )
    select_endpoint = next(
        (
            endpoint
            for endpoint in endpoints
            if isinstance(endpoint, dict)
            and endpoint.get("method") == "POST"
            and endpoint.get("path") == TUI_SELECT_SESSION_CONSUMED_PATH
        ),
        None,
    )
    if select_endpoint is None:
        raise UpgradeReceiptError(
            f"consumed_api_contract lacks POST {TUI_SELECT_SESSION_CONSUMED_PATH}"
        )
    if (
        select_endpoint.get("request_fields") != ["sessionID"]
        or select_endpoint.get("response_fields") != []
    ):
        raise UpgradeReceiptError(
            "consumed POST /tui/select-session must declare exactly "
            "request_fields=['sessionID'] and response_fields=[]"
        )
    prompt_endpoint = next(
        (
            endpoint
            for endpoint in endpoints
            if isinstance(endpoint, dict)
            and endpoint.get("method") == "POST"
            and endpoint.get("path") == PROMPT_ASYNC_CONSUMED_PATH
        ),
        None,
    )
    if prompt_endpoint is None:
        raise UpgradeReceiptError(
            f"consumed_api_contract lacks POST {PROMPT_ASYNC_CONSUMED_PATH}"
        )
    if (
        prompt_endpoint.get("request_fields") != ["parts"]
        or prompt_endpoint.get("response_fields") != []
    ):
        raise UpgradeReceiptError(
            f"consumed POST {PROMPT_ASYNC_CONSUMED_PATH} must declare "
            "exactly request_fields=['parts'] and response_fields=[]"
        )
    return [create_endpoint, select_endpoint, prompt_endpoint]


def observation_receipt_digest(receipt: dict[str, Any]) -> str:
    """Digest of a receipt, excluding any embedded digest field."""

    payload = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    return _sha256_hex(payload)


def _normalize_capability(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UpgradeReceiptError("capability must be an object")
    schema_digest = raw.get("schema_digest")
    if not _is_sha256(schema_digest):
        raise UpgradeReceiptError("capability.schema_digest must be a sha256 hex digest")
    return {
        "endpoint": _require_text(raw.get("endpoint"), "capability.endpoint"),
        "runtime_version": _require_text(
            raw.get("runtime_version"), "capability.runtime_version"
        ),
        "schema_digest": schema_digest,
    }


def _normalize_binding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise UpgradeReceiptError("binding must be an object")
    process = raw.get("process")
    if not isinstance(process, dict):
        raise UpgradeReceiptError("binding.process must be an object")
    pid = process.get("pid")
    if type(pid) is not int or pid <= 0:
        raise UpgradeReceiptError("binding.process.pid must be a positive integer")
    binding_generation = raw.get("binding_generation")
    if type(binding_generation) is not int or binding_generation < 0:
        raise UpgradeReceiptError("binding.binding_generation must be a non-negative integer")
    return {
        "worker_id": _require_text(raw.get("worker_id"), "binding.worker_id"),
        "session_id": _require_text(raw.get("session_id"), "binding.session_id"),
        "binding_generation": binding_generation,
        "process": {
            "pid": pid,
            "start_time": _require_text(
                process.get("start_time"), "binding.process.start_time"
            ),
        },
        "endpoint": _require_text(raw.get("endpoint"), "binding.endpoint"),
        "run_id": _require_text(raw.get("run_id"), "binding.run_id"),
        "task_id": _require_text(raw.get("task_id"), "binding.task_id"),
        "attempt_id": _require_text(raw.get("attempt_id"), "binding.attempt_id"),
    }


def build_observation_receipt(
    *,
    bundle_digest: str,
    scope: str,
    observed_at: str,
    compared_fields: list[str],
    consistent: bool,
    artifact_refs: list[str],
    capability: dict[str, Any] | None = None,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one append-only shadow/canary observation receipt.

    ``unbound_capability`` proves endpoint/version/schema capability only and
    must not carry a worker-scoped binding. ``bound_shadow`` requires the
    frozen worker, session, binding generation, process identity, endpoint,
    and task/attempt snapshot.
    """

    if scope not in OBSERVATION_SCOPES:
        raise UpgradeReceiptError(f"scope must be one of {OBSERVATION_SCOPES}")
    if not _is_sha256(bundle_digest):
        raise UpgradeReceiptError("bundle_digest must be a sha256 hex digest")
    if type(consistent) is not bool:
        raise UpgradeReceiptError("consistent must be boolean")
    if scope == "unbound_capability":
        if binding is not None:
            raise UpgradeReceiptError(
                "unbound_capability observations must not claim a worker binding"
            )
        if capability is None:
            raise UpgradeReceiptError("unbound_capability observations require capability")
    if scope == "bound_shadow" and binding is None:
        raise UpgradeReceiptError("bound_shadow observations require a frozen binding")
    receipt = {
        "receipt_kind": OBSERVATION_RECEIPT_KIND,
        "schema_version": UPGRADE_RECEIPT_SCHEMA_VERSION,
        "bundle_digest": bundle_digest,
        "scope": scope,
        "observed_at": _require_text(observed_at, "observed_at"),
        "capability": _normalize_capability(capability) if capability is not None else None,
        "binding": _normalize_binding(binding) if binding is not None else None,
        "compared_fields": _require_text_list(compared_fields, "compared_fields"),
        "consistent": consistent,
        "artifact_refs": _require_text_list(artifact_refs, "artifact_refs"),
    }
    if not receipt["compared_fields"] or not receipt["artifact_refs"]:
        raise UpgradeReceiptError(
            "compared_fields and artifact_refs must be non-empty lists"
        )
    receipt["receipt_digest"] = observation_receipt_digest(receipt)
    return receipt


_OBSERVATION_RECEIPT_FIELDS = frozenset(
    (
        "receipt_kind",
        "schema_version",
        "bundle_digest",
        "scope",
        "observed_at",
        "capability",
        "binding",
        "compared_fields",
        "consistent",
        "artifact_refs",
        "receipt_digest",
    )
)


def validate_observation_receipt(receipt: Any) -> dict[str, Any]:
    """Fully validate one observation receipt at a trust boundary.

    Rejects self-consistent but illegal receipts: wrong kind or schema,
    missing/extra fields, unknown scope, missing capability/binding or
    incomplete frozen identity for the declared scope, empty or
    non-canonical evidence lists, non-boolean consistency, and stale
    digests. Contract validation runs before digest verification.
    """

    if not isinstance(receipt, dict):
        raise UpgradeReceiptError("receipt must be an object")
    fields = set(receipt)
    if fields != _OBSERVATION_RECEIPT_FIELDS:
        missing = sorted(_OBSERVATION_RECEIPT_FIELDS - fields)
        extra = sorted(fields - _OBSERVATION_RECEIPT_FIELDS)
        raise UpgradeReceiptError(
            f"receipt fields mismatch: missing={missing} extra={extra}"
        )
    if receipt["receipt_kind"] != OBSERVATION_RECEIPT_KIND:
        raise UpgradeReceiptError("receipt_kind mismatch")
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != UPGRADE_RECEIPT_SCHEMA_VERSION
    ):
        raise UpgradeReceiptError("schema_version mismatch")
    if not _is_sha256(receipt["bundle_digest"]):
        raise UpgradeReceiptError("bundle_digest must be a sha256 hex digest")
    scope = receipt["scope"]
    if scope not in OBSERVATION_SCOPES:
        raise UpgradeReceiptError(f"scope must be one of {OBSERVATION_SCOPES}")
    _require_text(receipt["observed_at"], "observed_at")
    capability = receipt["capability"]
    binding = receipt["binding"]
    if capability is not None and capability != _normalize_capability(capability):
        raise UpgradeReceiptError("capability is not in canonical form")
    if binding is not None and binding != _normalize_binding(binding):
        raise UpgradeReceiptError("binding is not in canonical form")
    if scope == "unbound_capability":
        if binding is not None:
            raise UpgradeReceiptError(
                "unbound_capability observations must not claim a worker binding"
            )
        if capability is None:
            raise UpgradeReceiptError("unbound_capability observations require capability")
    if scope == "bound_shadow" and binding is None:
        raise UpgradeReceiptError("bound_shadow observations require a frozen binding")
    _require_evidence_list(receipt["compared_fields"], "compared_fields")
    if type(receipt["consistent"]) is not bool:
        raise UpgradeReceiptError("consistent must be boolean")
    _require_evidence_list(receipt["artifact_refs"], "artifact_refs")
    digest = receipt["receipt_digest"]
    if not _is_sha256(digest) or digest != observation_receipt_digest(receipt):
        raise UpgradeReceiptError("receipt_digest does not match receipt content")
    return receipt


@contextmanager
def _ledger_lock(ledger_path: Path) -> Iterator[None]:
    """Hold one cross-process lock bound to a single observation ledger."""

    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_observation_receipt(ledger_path: Path, receipt: dict[str, Any]) -> Path:
    """Append one receipt as a canonical JSON line to the ledger.

    The full receipt contract is re-validated before appending. The ledger
    is append-only: this function never rewrites, truncates, or reorders
    existing lines. The duplicate check and the append run inside one
    cross-process critical section (a private fcntl lock file bound to the
    ledger), so concurrent calls cannot create duplicate lines. An exact
    duplicate (an existing line with the same receipt digest) is skipped
    idempotently; any content difference is appended as a new observation.
    """

    receipt = validate_observation_receipt(receipt)
    ledger_path = Path(ledger_path)
    with _ledger_lock(ledger_path):
        existing_digests = {
            existing["receipt_digest"]
            for existing in read_observation_receipts(ledger_path)
        }
        if receipt["receipt_digest"] in existing_digests:
            return ledger_path
        line = json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return ledger_path


def read_observation_receipts(ledger_path: Path) -> list[dict[str, Any]]:
    """Read back every receipt line, failing closed on any contract or
    digest violation."""

    ledger_path = Path(ledger_path)
    if not ledger_path.is_file():
        return []
    receipts: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        ledger_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as exc:
            raise UpgradeReceiptError(
                f"observation ledger line {line_number} is malformed"
            ) from exc
        try:
            receipts.append(validate_observation_receipt(receipt))
        except UpgradeReceiptError as exc:
            raise UpgradeReceiptError(
                f"observation ledger line {line_number} is invalid: {exc}"
            ) from exc
    return receipts
