from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Mapping as MappingABC
from pathlib import Path
from typing import Any, Mapping


RUNTIME_IDENTITY = "visible-agent-orchestrator-runtime-v1"
RECEIPT_NAME = "runtime-identity.json"
MAX_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class RuntimeIdentityError(ValueError):
    pass


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _reject_constant(token: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {token}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or unicodedata.normalize("NFC", value) != value:
        raise RuntimeIdentityError(f"{label} is invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise RuntimeIdentityError(f"{label} is invalid") from exc
    return value


def _digest(value: Any, label: str) -> str:
    text = _text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise RuntimeIdentityError(f"{label} is invalid")
    return text


def receipt_path(control_root: str) -> Path:
    if not isinstance(control_root, str):
        raise RuntimeIdentityError("control root is invalid")
    raw = control_root
    try:
        raw.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise RuntimeIdentityError("control root is invalid") from exc
    if (
        not raw.startswith("/")
        or raw.startswith("//")
        or "//" in raw
        or "\x00" in raw
        or unicodedata.normalize("NFC", raw) != raw
        or (raw != "/" and any(part in {"", ".", ".."} for part in raw.split("/")[1:]))
    ):
        raise RuntimeIdentityError("control root is invalid")
    return Path(raw) / RECEIPT_NAME


def _validated(receipt: Mapping[str, Any], *, require_digest: bool) -> dict[str, Any]:
    try:
        if not isinstance(receipt, MappingABC):
            raise RuntimeIdentityError("receipt structure is invalid")
        payload = dict(receipt)
    except (TypeError, ValueError) as exc:
        raise RuntimeIdentityError("receipt structure is invalid") from exc
    expected = {
        "schema_version", "runtime_identity", "source_commit", "manifest_sha256", "contract_generation",
        "target_root", "targets", "install_event_id", "install_event_source_commit", "tree_digest",
        "runtime_attestation", "receipt_digest",
    }
    if set(payload) != expected or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise RuntimeIdentityError("receipt schema is invalid")
    if payload.get("runtime_identity") != RUNTIME_IDENTITY or payload.get("runtime_attestation") is not False:
        raise RuntimeIdentityError("receipt protocol is invalid")
    if type(payload.get("contract_generation")) is not int or payload["contract_generation"] < 1:
        raise RuntimeIdentityError("contract generation is invalid")
    if _COMMIT.fullmatch(_text(payload.get("source_commit"), "source commit")) is None:
        raise RuntimeIdentityError("source commit is invalid")
    if _COMMIT.fullmatch(_text(payload.get("install_event_source_commit"), "event source commit")) is None:
        raise RuntimeIdentityError("event source commit is invalid")
    if payload["install_event_source_commit"] != payload["source_commit"]:
        raise RuntimeIdentityError("event source commit mismatch")
    _text(payload.get("install_event_id"), "install event id")
    _digest(payload.get("manifest_sha256"), "manifest digest")
    _digest(payload.get("tree_digest"), "tree digest")
    target_root = _text(payload.get("target_root"), "target root")
    if (
        not Path(target_root).is_absolute()
        or target_root.startswith("//")
        or "//" in target_root
        or Path(target_root).as_posix() != target_root
        or any(part in {"", ".", ".."} for part in Path(target_root).parts[1:])
    ):
        raise RuntimeIdentityError("target root is invalid")
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise RuntimeIdentityError("target closure is invalid")
    previous = ""
    tree_entries: list[dict[str, Any]] = []
    for item in targets:
        if not isinstance(item, dict) or not isinstance(item.get("target_rel"), str) or (
            set(item) != {"target_rel", "expected_sha256", "current_sha256", "mode"}
            and set(item) != {"target_rel", "retired", "removed_sha256"}
        ):
            raise RuntimeIdentityError("target entry is invalid")
        relative = _text(item.get("target_rel"), "target relative path")
        path = Path(relative)
        if (
            path.is_absolute()
            or "//" in relative
            or path.as_posix() != relative
            or not path.parts
            or any(part in {"", ".", ".."} for part in Path(relative).parts)
            or relative <= previous
        ):
            raise RuntimeIdentityError("target closure ordering is invalid")
        previous = relative
        if item.get("retired") is True:
            removed_digest = _digest(item.get("removed_sha256"), "removed target digest")
            tree_entries.append(
                {
                    "target_rel": relative,
                    "retired": True,
                    "removed_sha256": removed_digest,
                }
            )
            continue
        expected_digest = _digest(item.get("expected_sha256"), "expected target digest")
        current_digest = _digest(item.get("current_sha256"), "current target digest")
        if expected_digest != current_digest or type(item.get("mode")) is not int or item["mode"] not in {0o644, 0o755}:
            raise RuntimeIdentityError("target entry identity is invalid")
        tree_entries.append(dict(item))
    tree_digest = "sha256:" + hashlib.sha256(_canonical({"target_root": target_root, "targets": tree_entries})).hexdigest()
    if payload["tree_digest"] != tree_digest:
        raise RuntimeIdentityError("tree digest mismatch")
    base = {key: value for key, value in payload.items() if key != "receipt_digest"}
    base["targets"] = tree_entries
    expected_receipt_digest = "sha256:" + hashlib.sha256(_canonical(base)).hexdigest()
    if require_digest and payload.get("receipt_digest") != expected_receipt_digest:
        raise RuntimeIdentityError("receipt digest mismatch")
    return {**base, "receipt_digest": expected_receipt_digest}


def build_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "runtime_identity", "source_commit", "manifest_sha256", "contract_generation",
        "target_root", "targets", "install_event_id", "install_event_source_commit", "runtime_attestation",
    }
    optional = {"tree_digest", "receipt_digest"}
    try:
        if not isinstance(payload, MappingABC):
            raise RuntimeIdentityError("receipt builder structure is invalid")
        candidate = dict(payload)
        if set(candidate) - (required | optional) or not required.issubset(candidate):
            raise RuntimeIdentityError("receipt builder fields are invalid")
        if not isinstance(candidate["targets"], list) or not all(isinstance(item, MappingABC) for item in candidate["targets"]):
            raise RuntimeIdentityError("receipt builder structure is invalid")
        candidate["targets"] = [dict(item) for item in candidate["targets"]]
        tree_digest = "sha256:" + hashlib.sha256(_canonical({"target_root": candidate.get("target_root"), "targets": candidate.get("targets")})).hexdigest()
        if "tree_digest" in candidate and candidate["tree_digest"] != tree_digest:
            raise RuntimeIdentityError("tree digest mismatch")
        candidate["tree_digest"] = tree_digest
        receipt_digest = "sha256:" + hashlib.sha256(_canonical({key: value for key, value in candidate.items() if key != "receipt_digest"})).hexdigest()
        if "receipt_digest" in candidate and candidate["receipt_digest"] != receipt_digest:
            raise RuntimeIdentityError("receipt digest mismatch")
        candidate["receipt_digest"] = receipt_digest
        return _validated(candidate, require_digest=True)
    except RuntimeIdentityError:
        raise
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise RuntimeIdentityError("receipt builder structure is invalid") from exc


def _control_directory(control_root: str) -> int:
    receipt_path(control_root)
    descriptor = -1
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in (() if control_root == "/" else control_root.split("/")[1:]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise RuntimeIdentityError("control root is unsafe") from exc
    observed = os.fstat(descriptor)
    if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        os.close(descriptor)
        raise RuntimeIdentityError("control root is unsafe")
    return descriptor


def write_receipt(control_root: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    validated = _validated(receipt, require_digest=True)
    encoded = _canonical(validated)
    if len(encoded) > MAX_BYTES:
        raise RuntimeIdentityError("runtime identity receipt is oversized")
    parent = _control_directory(control_root)
    temporary = f".{RECEIPT_NAME}.{uuid.uuid4().hex}.tmp"
    committed = False
    try:
        try:
            existing = os.open(RECEIPT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            existing = -1
        if existing >= 0:
            try:
                observed = os.fstat(existing)
                if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
                    raise RuntimeIdentityError("runtime identity receipt is unsafe")
            finally:
                os.close(existing)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            os.fchmod(descriptor, 0o600)
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise RuntimeIdentityError("receipt temporary mode is unsafe")
            view = memoryview(encoded)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise RuntimeIdentityError("receipt publication failed")
                view = view[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, RECEIPT_NAME, src_dir_fd=parent, dst_dir_fd=parent)
        committed = True
        os.fsync(parent)
    except (OSError, RuntimeIdentityError) as exc:
        try:
            os.unlink(temporary, dir_fd=parent)
        except OSError:
            pass
        if committed:
            raise RuntimeIdentityError("receipt committed but parent durability is unknown") from exc
        if isinstance(exc, RuntimeIdentityError):
            raise
        raise RuntimeIdentityError("receipt publication failed") from exc
    finally:
        os.close(parent)
    return validated


def read_receipt(control_root: str) -> dict[str, Any]:
    parent = _control_directory(control_root)
    try:
        descriptor = os.open(RECEIPT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
                raise RuntimeIdentityError("runtime identity receipt is unsafe")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(descriptor, min(65536, MAX_BYTES - total + 1)):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeIdentityError("runtime identity receipt is oversized")
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RuntimeIdentityError("runtime identity receipt is unsafe") from exc
    finally:
        os.close(parent)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeIdentityError("runtime identity receipt is invalid") from exc
    if not isinstance(payload, dict) or _canonical(payload) != raw:
        raise RuntimeIdentityError("runtime identity receipt is noncanonical")
    return _validated(payload, require_digest=True)
