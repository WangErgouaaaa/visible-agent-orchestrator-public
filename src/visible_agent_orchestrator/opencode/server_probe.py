"""Unbound read-only OpenCode server capability probe.

This probe issues GET requests only. It reads ``/global/health`` for a
non-empty runtime version and ``/doc`` for the OpenAPI document, and may
read ``/session/status`` as advisory telemetry only. It derives a
deterministic digest from the actually consumed paths and methods, never
from the whole document.

It never claims a worker, task, attempt, session, binding, or terminal
truth. It has no generic request(method, path) escape hatch, no submit API,
and performs no POST/PUT/DELETE, TUI prompt mutation, tmux input, process
launch, install/sync, activation, or registry write. Missing or malformed
required endpoints produce a structured fail-closed result.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


PROBE_KIND = "opencode_unbound_server_capability_probe"
PROBE_SCHEMA_VERSION = 1
HEALTH_PATH = "/global/health"
DOC_PATH = "/doc"
SESSION_STATUS_PATH = "/session/status"
CONSUMED_PATHS = (HEALTH_PATH, DOC_PATH, SESSION_STATUS_PATH)
REQUIRED_PATHS = (HEALTH_PATH,)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _get_json(url: str, timeout: float) -> tuple[Any, str]:
    """GET one URL and parse a JSON body; return (payload, failure_reason)."""

    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return None, f"connection_error:{exc.__class__.__name__}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    return payload, ""


def consumed_contract(endpoints: list[dict[str, str]]) -> dict[str, Any]:
    """Normalize consumed GET endpoints and digest only methods and paths."""

    normalized = sorted(
        ({"method": endpoint["method"].upper(), "path": endpoint["path"]}
         for endpoint in endpoints),
        key=lambda endpoint: (endpoint["method"], endpoint["path"]),
    )
    canonical = json.dumps(
        {"endpoints": normalized},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "endpoints": normalized,
        "contract_digest": hashlib.sha256(canonical).hexdigest(),
    }


def _valid_openapi_root(doc: dict[str, Any]) -> bool:
    """Check the minimal OpenAPI root shape this probe relies on."""

    openapi_version = doc.get("openapi")
    if not isinstance(openapi_version, str) or not openapi_version.strip():
        return False
    info = doc.get("info")
    if not isinstance(info, dict):
        return False
    for field in ("title", "version"):
        value = info.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    return True


def probe_opencode_server(endpoint: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """Probe one OpenCode server endpoint, unbound and read-only.

    Returns a structured result. ``ok`` is true only when every required
    endpoint answered with well-formed content; the result carries no
    worker, task, session, binding, or terminal-truth claim.
    """

    failures: list[dict[str, str]] = []
    advisory_failures: list[dict[str, str]] = []

    base = endpoint.rstrip("/") if isinstance(endpoint, str) else ""
    if not base.startswith(("http://", "https://")):
        return {
            "probe_kind": PROBE_KIND,
            "schema_version": PROBE_SCHEMA_VERSION,
            "endpoint": endpoint,
            "probed_at": _utc_now(),
            "ok": False,
            "runtime_version": "",
            "consumed_api_contract": {"endpoints": [], "contract_digest": ""},
            "session_status_advisory": None,
            "failures": [
                {"endpoint": "<endpoint>", "reason": "endpoint_scheme_invalid"}
            ],
            "advisory_failures": [],
        }

    health, health_error = _get_json(f"{base}{HEALTH_PATH}", timeout)
    runtime_version = ""
    if health_error:
        failures.append({"endpoint": HEALTH_PATH, "reason": health_error})
    elif not isinstance(health, dict):
        failures.append({"endpoint": HEALTH_PATH, "reason": "not_a_json_object"})
    else:
        version = health.get("version")
        if not isinstance(version, str) or not version.strip():
            failures.append({"endpoint": HEALTH_PATH, "reason": "version_missing"})
        else:
            runtime_version = version

    doc, doc_error = _get_json(f"{base}{DOC_PATH}", timeout)
    paths: dict[str, Any] = {}
    paths_checked = False
    doc_consumed = False
    if doc_error:
        failures.append({"endpoint": DOC_PATH, "reason": doc_error})
    elif not isinstance(doc, dict):
        failures.append({"endpoint": DOC_PATH, "reason": "openapi_paths_missing"})
    else:
        if not _valid_openapi_root(doc):
            failures.append({"endpoint": DOC_PATH, "reason": "openapi_root_invalid"})
        doc_paths = doc.get("paths")
        if not isinstance(doc_paths, dict):
            failures.append({"endpoint": DOC_PATH, "reason": "openapi_paths_missing"})
        else:
            paths = doc_paths
            paths_checked = True
            doc_consumed = True

    declared_get: set[str] = set()
    malformed_get: set[str] = set()
    for path in CONSUMED_PATHS:
        entry = paths.get(path)
        if not isinstance(entry, dict) or "get" not in entry:
            continue
        if isinstance(entry["get"], dict):
            declared_get.add(path)
        else:
            malformed_get.add(path)
    if paths_checked:
        for path in (HEALTH_PATH, DOC_PATH):
            if path in malformed_get:
                failures.append(
                    {"endpoint": path, "reason": "operation_get_not_object"}
                )
        for path in REQUIRED_PATHS:
            if path in malformed_get:
                continue
            if path not in declared_get:
                failures.append(
                    {"endpoint": path, "reason": "consumed_path_not_declared_get"}
                )

    session_status: dict[str, Any] | None = None
    if SESSION_STATUS_PATH in declared_get and not failures:
        status, status_error = _get_json(f"{base}{SESSION_STATUS_PATH}", timeout)
        if status_error:
            advisory_failures.append(
                {"endpoint": SESSION_STATUS_PATH, "reason": status_error}
            )
        elif not isinstance(status, dict):
            advisory_failures.append(
                {"endpoint": SESSION_STATUS_PATH, "reason": "not_a_json_object"}
            )
        else:
            session_status = status
    elif not failures:
        advisory_failures.append(
            {"endpoint": SESSION_STATUS_PATH, "reason": "consumed_path_not_declared_get"}
        )

    consumed = []
    for path in CONSUMED_PATHS:
        if path == DOC_PATH:
            if doc_consumed:
                consumed.append({"method": "GET", "path": path})
        elif path in declared_get:
            consumed.append({"method": "GET", "path": path})
    contract = consumed_contract(consumed) if not failures else {
        "endpoints": [],
        "contract_digest": "",
    }
    return {
        "probe_kind": PROBE_KIND,
        "schema_version": PROBE_SCHEMA_VERSION,
        "endpoint": base,
        "probed_at": _utc_now(),
        "ok": not failures,
        "runtime_version": runtime_version,
        "consumed_api_contract": contract,
        "session_status_advisory": session_status,
        "failures": failures,
        "advisory_failures": advisory_failures,
    }
