"""Fail-closed operation capability, ledger, and receipt contracts."""  # noqa: SIZE_OK - task allowlist forbids a second source module

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Iterator, Mapping

from visible_agent_orchestrator.upgrade_receipt import (
    UpgradeReceiptError,
    require_consumed_server_cold_start_contract,
    require_consumed_server_prompt_async_contract,
    require_consumed_server_supplement_contract,
    resolve_pinned_candidate_bundle_manifest,
)


class OperationAuthorizationError(ValueError):
    """Raised when an operation authorization boundary is not trustworthy."""


_EFFECT_GATES: Final[dict[str, tuple[str, ...]]] = {
    "worker_private_task": ("task_directory_write",),
    "worker_transport": ("worker_transport",),
    "worker_binding": ("worker_binding",),
    "session_rotation": ("session_rotation_authorization",),
    "repo_ref_write": ("repo_ref_write",),
    "installed_runtime_write": ("install_sync",),
    "global_registry_write": ("global_registry",),
    "remote_write": ("remote_mutation",),
}
_OPERATION_ENVELOPES: Final[dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "worker_dispatch": (
        "worker",
        ("dispatch_authorization", "target_worker_lock"),
        (
            "worker_private_task",
            "worker_transport",
            "worker_binding",
            "session_rotation",
        ),
    ),
    "worker_rebind": (
        "worker",
        ("rebind_authorization", "target_worker_lock"),
        ("worker_binding",),
    ),
    "worker_supplement": (
        "worker",
        ("supplement_authorization", "target_worker_lock"),
        ("worker_private_task", "worker_transport"),
    ),
    "worker_interrupt": (
        "worker",
        ("interrupt_authorization", "target_worker_lock"),
        ("worker_private_task", "worker_transport"),
    ),
    "git_ref_update": (
        "repository",
        ("repo_ref_authorization",),
        ("repo_ref_write",),
    ),
    "install_sync": (
        "install_root",
        ("global_zero_lock", "install_authorization"),
        ("installed_runtime_write",),
    ),
    "global_registry_mutation": (
        "global_registry",
        ("global_zero_lock", "registry_authorization"),
        ("global_registry_write",),
    ),
}
_UNSUPPORTED_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "read",
        "write",
        "delete",
        "git_commit",
        "remote_write",
        "session_rotate",
        "runtime_start_stop",
    }
)
_TRUSTED_RECEIPT_OBSERVER: Final[dict[str, str]] = {
    "kind": "wrapper",
    "id": "omx_visible_dispatch",
}
_EXTERNAL_HARNESS_SOURCE: Final[str] = "external_harness_v1"
_CAPABILITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "contract_id",
        "contract_hash",
        "authorization_id",
        "authorization_source",
        "operation_kind",
        "scope",
        "mutation_gates",
        "task_id",
        "attempt_id",
    }
)
_RECEIPT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "receipt_id",
        "authorization_id",
        "operation_kind",
        "task_id",
        "attempt_id",
        "ledger_sequence",
        "resolved_target",
        "before_state",
        "after_state",
        "trusted_observer",
        "outcome",
        "created_at",
    }
)
_KNOWN_GATES: Final[frozenset[str]] = frozenset(
    gate
    for _scope, baseline, effects in _OPERATION_ENVELOPES.values()
    for gate in (*baseline, *[gate for effect in effects for gate in _EFFECT_GATES[effect]])
)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationAuthorizationError(f"{label} must be an object")
    return dict(value)


def _string(value: Any, label: str) -> str:
    text = value if isinstance(value, str) else ""
    if not text.strip():
        raise OperationAuthorizationError(f"{label} is required")
    return text.strip()


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise OperationAuthorizationError(f"{label} must be a string list")
    if len(value) != len(set(value)):
        raise OperationAuthorizationError(f"{label} contains duplicates")
    return list(value)


def _sha256(value: Any, label: str) -> str:
    text = _string(value, label)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise OperationAuthorizationError(f"{label} must be sha256:<hex>")
    return text


def _rfc3339(value: Any, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationAuthorizationError(
            f"{label} must be RFC3339"
        ) from exc
    if parsed.tzinfo is None:
        raise OperationAuthorizationError(f"{label} must be RFC3339")
    return text


def _exact_fields(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    if set(payload) != expected:
        raise OperationAuthorizationError(f"{label} fields are invalid")


def derive_operation_contract(
    operation_kind: str,
    task_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the wrapper-owned envelope; task data may not lower it."""
    if operation_kind in _UNSUPPORTED_OPERATIONS:
        raise OperationAuthorizationError("unsupported operation in v1")
    envelope = _OPERATION_ENVELOPES.get(operation_kind)
    if envelope is None:
        raise OperationAuthorizationError(f"unknown operation: {operation_kind}")
    scope_kind, baseline_gates, known_effects = envelope
    constraints = {}
    if task_contract is not None:
        payload = _mapping(task_contract, "task contract")
        candidate = payload.get("operation_constraints", {})
        constraints = _mapping(candidate, "operation constraints")
    declared_effects = constraints.get("known_effects")
    if declared_effects is not None:
        effects = _strings(declared_effects, "known effects")
        unknown = sorted(set(effects) - set(_EFFECT_GATES))
        if unknown:
            raise OperationAuthorizationError(f"unknown effect: {unknown[0]}")
        if set(effects) != set(known_effects):
            raise OperationAuthorizationError("task contract cannot lower known effects")
    declared_gates = constraints.get("mutation_gates")
    required_gates = set(baseline_gates)
    for effect in known_effects:
        required_gates.update(_EFFECT_GATES[effect])
    if declared_gates is not None:
        gates = _strings(declared_gates, "mutation gates")
        unknown = sorted(set(gates) - _KNOWN_GATES)
        if unknown:
            raise OperationAuthorizationError(f"unknown mutation gate: {unknown[0]}")
        if not required_gates.issubset(gates):
            raise OperationAuthorizationError("task contract cannot lower mutation gates")
        required_gates.update(gates)
    return {
        "operation_kind": operation_kind,
        "scope_kind": scope_kind,
        "known_effects": list(known_effects),
        "required_mutation_gates": sorted(required_gates),
    }


def validate_mutation_gate_union(
    required_mutation_gates: list[str],
    applied_mutation_gates: list[str],
) -> None:
    required = set(
        _strings(required_mutation_gates, "required mutation gates")
    )
    applied = set(
        _strings(applied_mutation_gates, "applied mutation gates")
    )
    unknown = sorted((required | applied) - _KNOWN_GATES)
    if unknown:
        raise OperationAuthorizationError(
            f"unknown mutation gate: {unknown[0]}"
        )
    missing = sorted(required - applied)
    if missing:
        raise OperationAuthorizationError(
            f"mutation gate not enforced: {missing[0]}"
        )


def validate_operation_capability(
    capability: Mapping[str, Any],
    *,
    operation_kind: str,
    target_scope: Mapping[str, Any],
    required_mutation_gates: list[str],
    task_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Validate an externally supplied, ledger-backed operation capability."""
    payload = _mapping(capability, "operation capability")
    _exact_fields(payload, _CAPABILITY_FIELDS, "operation capability")
    if payload.get("schema_version") != 1:
        raise OperationAuthorizationError(
            "operation capability schema version is invalid"
        )
    _string(payload.get("contract_id"), "capability contract id")
    _sha256(payload.get("contract_hash"), "capability contract hash")
    if payload.get("authorization_source") != "authorization_ledger":
        raise OperationAuthorizationError("task prose cannot grant capabilities")
    _string(payload.get("authorization_id"), "authorization id")
    if payload.get("operation_kind") != operation_kind:
        raise OperationAuthorizationError("operation kind mismatch")
    if (
        _string(payload.get("task_id"), "capability task id") != _string(task_id, "task id")
        or _string(payload.get("attempt_id"), "capability attempt id")
        != _string(attempt_id, "attempt id")
    ):
        raise OperationAuthorizationError("capability task-attempt mismatch")
    scope = _mapping(payload.get("scope"), "capability scope")
    expected_scope = _mapping(target_scope, "target scope")
    _exact_fields(scope, frozenset({"kind", "id"}), "capability scope")
    if _canonical_json(scope) != _canonical_json(expected_scope):
        raise OperationAuthorizationError("scope mismatch")
    gates = _strings(payload.get("mutation_gates"), "capability mutation gates")
    unknown = sorted(set(gates) - _KNOWN_GATES)
    if unknown:
        raise OperationAuthorizationError(f"unknown mutation gate: {unknown[0]}")
    if set(required_mutation_gates) != set(gates):
        raise OperationAuthorizationError(
            "capability mutation gates do not match required gate union"
        )
    return payload


class AuthorizationLedger:
    """Durable append-only authorization decisions with a sequence boundary."""

    def __init__(
        self,
        path: Path | str,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        if (task_id is None) != (attempt_id is None):
            raise OperationAuthorizationError(
                "ledger task and attempt must be bound together"
            )
        self.path = Path(path)
        self.task_id = _string(task_id, "ledger task id") if task_id is not None else None
        self.attempt_id = (
            _string(attempt_id, "ledger attempt id")
            if attempt_id is not None
            else None
        )

    def records(self) -> list[dict[str, Any]]:
        descriptor, parent = self._open(create=False, writable=False)
        if descriptor is None:
            os.close(parent)
            return []
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            records, _valid_end, _torn_tail = self._parse(self._read(descriptor))
            return records
        finally:
            os.close(descriptor)
            os.close(parent)

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        descriptor, parent = self._open(create=True, writable=True)
        assert descriptor is not None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return self._append_locked(descriptor, parent, record)
        finally:
            os.close(descriptor)
            os.close(parent)

    def _append_locked(
        self,
        descriptor: int,
        parent: int,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        current, valid_end, torn_tail = self._parse(self._read(descriptor))
        if torn_tail:
            os.ftruncate(descriptor, valid_end)
            os.fsync(descriptor)
        candidate = dict(record)
        for index, existing in enumerate(current):
            if existing["authorization_id"] != candidate.get("authorization_id"):
                continue
            if any(
                record.get("revokes_authorization_id")
                == existing["authorization_id"]
                for record in current[index + 1 :]
            ):
                raise OperationAuthorizationError(
                    "authorization reuse is forbidden"
                )
            supplied_sequence = candidate.get("sequence")
            if supplied_sequence not in (None, existing["sequence"]):
                raise OperationAuthorizationError("replay conflict")
            replay = {
                **candidate,
                "created_at": existing["created_at"],
                "sequence": existing["sequence"],
            }
            if existing == replay:
                return existing
            raise OperationAuthorizationError("replay conflict")
        expected_sequence = len(current) + 1
        supplied_sequence = candidate.get("sequence")
        if supplied_sequence not in (None, expected_sequence):
            raise OperationAuthorizationError("sequence conflict")
        candidate["sequence"] = expected_sequence
        self._validate_record(candidate, current)
        line = (_canonical_json(candidate) + "\n").encode("ascii")
        os.lseek(descriptor, 0, os.SEEK_END)
        view = memoryview(line)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OperationAuthorizationError("authorization ledger write failed")
            view = view[written:]
        os.fsync(descriptor)
        os.fsync(parent)
        return candidate

    def boundary_sequence(self) -> int:
        return len(self.records())

    def active_authorization(
        self, authorization_id: str, *, boundary_sequence: int | None = None
    ) -> dict[str, Any] | None:
        records = self.records()
        boundary = len(records) if boundary_sequence is None else boundary_sequence
        if boundary < 0 or boundary > len(records):
            raise OperationAuthorizationError("authorization boundary is invalid")
        return self._active_authorization(
            records[:boundary],
            authorization_id,
        )

    @staticmethod
    def _active_authorization(
        records: list[dict[str, Any]],
        authorization_id: str,
    ) -> dict[str, Any] | None:
        active: dict[str, dict[str, Any]] = {}
        for record in records:
            match record["decision"]:
                case "grant":
                    active[record["authorization_id"]] = record
                case "revoke":
                    active.pop(record["revokes_authorization_id"], None)
                case unreachable:
                    raise AssertionError(f"unexpected ledger action: {unreachable}")
        grant = active.get(authorization_id)
        return dict(grant["capability"]) if grant else None

    def consume_authorization(
        self,
        authorization_id: str,
        *,
        principal: str = "",
        provenance: str = "omx_install_sync:bootstrap-companion-skills",
        expected_boundary: int | None = None,
    ) -> dict[str, Any] | None:
        descriptor, parent = self._open(create=False, writable=True)
        if descriptor is None:
            os.close(parent)
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            records, _valid_end, _torn_tail = self._parse(self._read(descriptor))
            if (
                expected_boundary is not None
                and len(records) != expected_boundary
            ):
                raise OperationAuthorizationError(
                    "authorization boundary changed"
                )
            active_grant: dict[str, Any] | None = None
            for record in records:
                if (
                    record["decision"] == "grant"
                    and record["authorization_id"] == authorization_id
                ):
                    active_grant = record
                elif (
                    record["decision"] == "revoke"
                    and record["revokes_authorization_id"] == authorization_id
                ):
                    active_grant = None
            if active_grant is None:
                return next(
                    (
                        record
                        for record in reversed(records)
                        if record["decision"] == "revoke"
                        and record.get("revokes_authorization_id")
                        == authorization_id
                    ),
                    None,
                )
            return self._append_locked(
                descriptor,
                parent,
                {
                    "schema_version": 1,
                    "authorization_id": (
                        f"consume-{authorization_id}-{uuid.uuid4().hex}"
                    ),
                    "principal": principal or self.task_id or "omx_install_sync",
                    "decision": "revoke",
                    "task_id": active_grant["task_id"],
                    "attempt_id": active_grant["attempt_id"],
                    "contract_hash": active_grant["contract_hash"],
                    "created_at": (
                        datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    ),
                    "provenance": provenance,
                    "revokes_authorization_id": authorization_id,
                },
            )
        finally:
            os.close(descriptor)
            os.close(parent)

    def require_active_authorization(
        self, authorization_id: str, *, expected_boundary: int
    ) -> dict[str, Any]:
        records = self.records()
        if len(records) != expected_boundary:
            raise OperationAuthorizationError("authorization boundary changed")
        capability = self._active_authorization(records, authorization_id)
        if capability is None:
            raise OperationAuthorizationError("authorization is inactive")
        return capability

    @contextmanager
    def mutation_boundary(
        self,
        authorization_id: str,
        *,
        expected_boundary: int,
    ) -> Iterator[dict[str, Any]]:
        """Hold one ledger snapshot until the caller commits mutation ownership."""
        descriptor, parent = self._open(create=False, writable=False)
        if descriptor is None:
            os.close(parent)
            raise OperationAuthorizationError("authorization is inactive")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            records, _valid_end, _torn_tail = self._parse(self._read(descriptor))
            if len(records) != expected_boundary:
                raise OperationAuthorizationError("authorization boundary changed")
            capability = self._active_authorization(records, authorization_id)
            if capability is None:
                raise OperationAuthorizationError("authorization is inactive")
            yield capability
        finally:
            os.close(descriptor)
            os.close(parent)

    def _parse(
        self,
        raw: bytes,
    ) -> tuple[list[dict[str, Any]], int, bytes]:
        records: list[dict[str, Any]] = []
        lines = raw.splitlines(keepends=True)
        valid_end = 0
        for index, raw_line in enumerate(lines):
            is_last = index == len(lines) - 1
            complete = raw_line.endswith(b"\n")
            line = raw_line.rstrip(b"\r\n")
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if (
                    is_last
                    and not complete
                    and isinstance(exc, json.JSONDecodeError)
                    and exc.pos >= len(line)
                ):
                    return records, valid_end, raw_line
                raise OperationAuthorizationError("invalid JSONL record") from exc
            record = _mapping(payload, "ledger record")
            if _canonical_json(record).encode("ascii") != line:
                raise OperationAuthorizationError("ledger record is not canonical JSON")
            self._validate_record(record, records)
            records.append(record)
            valid_end += len(raw_line)
        return records, valid_end, b""

    @staticmethod
    def _read(descriptor: int) -> bytes:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _open(
        self,
        *,
        create: bool,
        writable: bool,
    ) -> tuple[int | None, int]:
        path = self.path if self.path.is_absolute() else Path.cwd() / self.path
        parts = path.parts
        if not parts or path.anchor != "/":
            raise OperationAuthorizationError("authorization ledger path must be absolute")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open("/", directory_flags)
        try:
            for part in parts[1:-1]:
                try:
                    child = os.open(part, directory_flags, dir_fd=parent)
                except FileNotFoundError:
                    if not create:
                        return None, parent
                    try:
                        os.mkdir(part, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    os.fsync(parent)
                    child = os.open(part, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            flags = os.O_NOFOLLOW | os.O_NONBLOCK
            flags |= os.O_RDWR if writable else os.O_RDONLY
            try:
                descriptor = os.open(parts[-1], flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    return None, parent
                try:
                    descriptor = os.open(
                        parts[-1],
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent,
                    )
                except FileExistsError:
                    descriptor = os.open(parts[-1], flags, dir_fd=parent)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(descriptor)
                raise OperationAuthorizationError(
                    "authorization ledger is not a regular file"
                )
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
                os.close(descriptor)
                raise OperationAuthorizationError(
                    "authorization ledger must be an owner-controlled file"
                )
            return descriptor, parent
        except (OSError, OperationAuthorizationError) as exc:
            os.close(parent)
            if isinstance(exc, OperationAuthorizationError):
                raise
            raise OperationAuthorizationError(
                "authorization ledger is not a regular file"
            ) from exc

    def _validate_record(self, record: Mapping[str, Any], prior: list[dict[str, Any]]) -> None:
        decision = record.get("decision")
        base_fields = frozenset(
            {
                "schema_version",
                "authorization_id",
                "sequence",
                "task_id",
                "attempt_id",
                "principal",
                "decision",
                "contract_hash",
                "created_at",
                "provenance",
            }
        )
        expected_fields = (
            base_fields | {"capability"}
            if decision == "grant"
            else base_fields | {"revokes_authorization_id"}
            if decision == "revoke"
            else base_fields
        )
        _exact_fields(record, frozenset(expected_fields), "ledger record")
        if record.get("schema_version") != 1:
            raise OperationAuthorizationError(
                "ledger schema version is invalid"
            )
        sequence = record.get("sequence")
        if type(sequence) is not int or sequence != len(prior) + 1:
            raise OperationAuthorizationError("sequence gap")
        authorization_id = _string(record.get("authorization_id"), "authorization id")
        if any(item["authorization_id"] == authorization_id for item in prior):
            raise OperationAuthorizationError("authorization id is not unique")
        task_id = _string(record.get("task_id"), "task id")
        attempt_id = _string(record.get("attempt_id"), "attempt id")
        expected_task_id = self.task_id or (
            str(prior[0]["task_id"]) if prior else task_id
        )
        expected_attempt_id = self.attempt_id or (
            str(prior[0]["attempt_id"]) if prior else attempt_id
        )
        if task_id != expected_task_id or attempt_id != expected_attempt_id:
            raise OperationAuthorizationError("ledger task-attempt mismatch")
        _string(record.get("principal"), "authorization principal")
        contract_hash = _sha256(
            record.get("contract_hash"),
            "authorization contract hash",
        )
        _rfc3339(record.get("created_at"), "authorization created at")
        _string(record.get("provenance"), "authorization provenance")
        match decision:
            case "grant":
                capability = _mapping(record.get("capability"), "grant capability")
                _exact_fields(
                    capability,
                    _CAPABILITY_FIELDS,
                    "operation capability",
                )
                if (
                    capability.get("authorization_id") != authorization_id
                    or capability.get("task_id") != task_id
                    or capability.get("attempt_id") != attempt_id
                    or capability.get("contract_hash") != contract_hash
                ):
                    raise OperationAuthorizationError("grant task-attempt mismatch")
            case "revoke":
                revoked_id = _string(record.get("revokes_authorization_id"), "revoked authorization id")
                grant = next((item for item in prior if item["authorization_id"] == revoked_id), None)
                if grant is None or grant["task_id"] != task_id or grant["attempt_id"] != attempt_id:
                    raise OperationAuthorizationError("revoke task-attempt mismatch")
            case _:
                raise OperationAuthorizationError("unknown ledger action")


def build_trusted_operation_receipt(
    *,
    authorization_id: str,
    operation_kind: str,
    resolved_target: Mapping[str, Any],
    before_state: Mapping[str, Any],
    after_state: Mapping[str, Any],
    outcome: str,
    task_id: str,
    attempt_id: str,
    ledger_sequence: int,
    created_at: str,
) -> dict[str, Any]:
    """Build a canonical receipt for an observer-visible operation outcome."""
    if type(ledger_sequence) is not int or ledger_sequence < 1:
        raise OperationAuthorizationError("ledger sequence is invalid")
    if operation_kind not in _OPERATION_ENVELOPES:
        raise OperationAuthorizationError("receipt operation is invalid")
    if outcome not in {"applied", "ambiguous", "rejected"}:
        raise OperationAuthorizationError("receipt outcome is invalid")
    payload = {
        "schema_version": 1,
        "receipt_kind": "trusted_operation_receipt",
        "authorization_id": _string(authorization_id, "authorization id"),
        "operation_kind": _string(operation_kind, "operation kind"),
        "task_id": _string(task_id, "task id"),
        "attempt_id": _string(attempt_id, "attempt id"),
        "ledger_sequence": ledger_sequence,
        "resolved_target": _mapping(resolved_target, "resolved target"),
        "before_state": _mapping(before_state, "before state"),
        "after_state": _mapping(after_state, "after state"),
        "trusted_observer": dict(_TRUSTED_RECEIPT_OBSERVER),
        "outcome": outcome,
        "created_at": _rfc3339(created_at, "receipt created at"),
    }
    payload["receipt_id"] = "sha256:" + hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()
    return payload


def validate_trusted_operation_receipt(
    receipt: Mapping[str, Any],
    *,
    authorization_id: str,
    operation_kind: str,
    resolved_target: Mapping[str, Any],
    task_id: str,
    attempt_id: str,
    ledger_sequence: int,
) -> dict[str, Any]:
    """Reject narrative-only and untrusted receipt claims without guessing outcome."""
    payload = _mapping(receipt, "operation receipt")
    _exact_fields(payload, _RECEIPT_FIELDS, "operation receipt")
    if payload.get("schema_version") != 1:
        raise OperationAuthorizationError("receipt schema version is invalid")
    observer = _mapping(payload.get("trusted_observer"), "trusted observer")
    if (
        observer != _TRUSTED_RECEIPT_OBSERVER
    ):
        raise OperationAuthorizationError("trusted observer is required")
    if payload.get("receipt_kind") != "trusted_operation_receipt":
        raise OperationAuthorizationError("receipt kind is invalid")
    if operation_kind not in _OPERATION_ENVELOPES:
        raise OperationAuthorizationError("receipt operation is invalid")
    if payload.get("authorization_id") != authorization_id or payload.get("operation_kind") != operation_kind:
        raise OperationAuthorizationError("receipt authorization or operation mismatch")
    if type(ledger_sequence) is not int or ledger_sequence < 1:
        raise OperationAuthorizationError("ledger sequence is invalid")
    if (
        payload.get("task_id") != task_id
        or payload.get("attempt_id") != attempt_id
        or payload.get("ledger_sequence") != ledger_sequence
    ):
        raise OperationAuthorizationError("receipt task-attempt boundary mismatch")
    target = _mapping(payload.get("resolved_target"), "receipt target")
    if _canonical_json(target) != _canonical_json(_mapping(resolved_target, "resolved target")):
        raise OperationAuthorizationError("receipt target mismatch")
    _mapping(payload.get("before_state"), "before state")
    _mapping(payload.get("after_state"), "after state")
    if payload.get("outcome") not in {"applied", "ambiguous", "rejected"}:
        raise OperationAuthorizationError("receipt outcome is invalid")
    _rfc3339(payload.get("created_at"), "receipt created at")
    unsigned = {key: value for key, value in payload.items() if key != "receipt_id"}
    expected = "sha256:" + hashlib.sha256(_canonical_json(unsigned).encode("ascii")).hexdigest()
    if payload.get("receipt_id") != expected:
        raise OperationAuthorizationError("receipt is not canonical")
    return payload


def _canonical_json_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _simple_id(value: Any, label: str) -> str:
    text = _string(value, label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text):
        raise OperationAuthorizationError(
            f"{label} is not a simple identifier"
        )
    if ".." in text:
        raise OperationAuthorizationError(
            f"{label} is not a simple identifier"
        )
    return text


OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD: Final = (
    "opencode_server_prompt_async_canary"
)

_OPENCODE_SERVER_PROMPT_ASYNC_CANARY_KEYS: Final = frozenset(
    (
        "candidate_manifest_path",
        "candidate_bundle_digest",
        "session_id",
    )
)
_OPENCODE_SERVER_PROMPT_ASYNC_COLD_START_KEYS: Final = frozenset(
    (
        "candidate_manifest_path",
        "candidate_bundle_digest",
        "session_mode",
    )
)


def extract_opencode_server_prompt_async_canary(
    task_contract: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Extract the optional Server prompt_async canary selector, fail closed.

    Absence returns ``None`` and preserves default TUI behavior. Presence
    requires exactly ``candidate_manifest_path`` (absolute),
    ``candidate_bundle_digest`` (sha256 hex), and ``session_id`` (safe
    ``ses_`` identifier); any other shape is rejected.
    """

    if task_contract is None:
        return None
    payload = _mapping(task_contract, "task contract")
    if OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD not in payload:
        return None
    selector = _mapping(
        payload.get(OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD),
        OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD,
    )
    keys = set(selector)
    valid_shapes = (
        _OPENCODE_SERVER_PROMPT_ASYNC_CANARY_KEYS,
        _OPENCODE_SERVER_PROMPT_ASYNC_COLD_START_KEYS,
    )
    if not any(keys == set(shape) for shape in valid_shapes):
        raise OperationAuthorizationError(
            f"{OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD} fields mismatch: "
            "expected either session_id or session_mode=create"
        )
    session: dict[str, str]
    if "session_id" in selector:
        session_id = _string(selector["session_id"], "selector session_id")
        if not re.fullmatch(r"ses_[A-Za-z0-9][A-Za-z0-9_.-]*", session_id):
            raise OperationAuthorizationError(
                "selector session_id is not a safe opencode session id"
            )
        session = {"session_id": session_id}
    else:
        session_mode = _string(selector["session_mode"], "selector session_mode")
        if session_mode != "create":
            raise OperationAuthorizationError(
                "selector session_mode must be 'create'"
            )
        session = {"session_mode": session_mode}
    digest = _string(
        selector["candidate_bundle_digest"],
        "selector candidate_bundle_digest",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise OperationAuthorizationError(
            "selector candidate_bundle_digest must be a sha256 hex digest"
        )
    manifest_path = _string(
        selector["candidate_manifest_path"],
        "selector candidate_manifest_path",
    )
    if not Path(manifest_path).is_absolute():
        raise OperationAuthorizationError(
            "selector candidate_manifest_path must be an absolute path"
        )
    return {
        "candidate_manifest_path": manifest_path,
        "candidate_bundle_digest": digest,
        **session,
    }


def admit_opencode_server_prompt_async_canary(
    selector: Mapping[str, str],
) -> dict[str, Any]:
    """Re-validate the pinned manifest for one Server canary selector.

    Resolves the pinned candidate bundle manifest (canonical regular
    file, full contract validation, exact bundle digest) and requires the
    consumed POST /session/{sessionID}/prompt_async contract with the
    ``parts`` request field. Runs before grant append and again before
    submission so a swapped or tampered manifest fails closed.
    """

    checked = extract_opencode_server_prompt_async_canary(
        {OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD: dict(selector)}
    )
    if checked is None:
        raise OperationAuthorizationError("selector is required for admission")
    try:
        manifest = resolve_pinned_candidate_bundle_manifest(
            checked["candidate_manifest_path"],
            expected_bundle_digest=checked["candidate_bundle_digest"],
        )
        if checked.get("session_mode") == "create":
            endpoint = require_consumed_server_cold_start_contract(manifest)
        else:
            endpoint = require_consumed_server_prompt_async_contract(manifest)
    except UpgradeReceiptError as exc:
        raise OperationAuthorizationError(str(exc)) from exc
    return {
        "selector": checked,
        "consumed_endpoint": endpoint,
        "manifest": manifest,
    }


def admit_opencode_server_supplement_canary(
    selector: Mapping[str, str],
) -> dict[str, Any]:
    """Re-validate the pinned manifest for one selection-capable supplement.

    Same resolver and bundle-digest rule as the dispatch canary, but the
    manifest must carry the selection-capable supplement projection; the
    parts-only dispatch receipt is not sufficient proof that the original
    agent/model/variant travel with the supplement.
    """

    checked = extract_opencode_server_prompt_async_canary(
        {OPENCODE_SERVER_PROMPT_ASYNC_CANARY_FIELD: dict(selector)}
    )
    if checked is None:
        raise OperationAuthorizationError("selector is required for admission")
    try:
        manifest = resolve_pinned_candidate_bundle_manifest(
            checked["candidate_manifest_path"],
            expected_bundle_digest=checked["candidate_bundle_digest"],
        )
        if checked.get("session_mode") == "create":
            raise UpgradeReceiptError(
                "cold-start session creation has no running task to supplement"
            )
        consumed = require_consumed_server_supplement_contract(manifest)
    except UpgradeReceiptError as exc:
        raise OperationAuthorizationError(str(exc)) from exc
    return {
        "selector": checked,
        "consumed_endpoint": consumed["endpoint"],
        "emitted_optional_fields": consumed["emitted_optional_fields"],
        "manifest": manifest,
    }


def require_active_external_harness_capability(
    root: Path,
    task_contract: Mapping[str, Any],
    *,
    task_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Revalidate the exact persisted harness grant before a cold-start effect."""

    contract = _mapping(task_contract, "task contract")
    capability = _mapping(
        contract.get("operation_capability"),
        "task contract operation_capability",
    )
    authorization_id = _string(
        capability.get("authorization_id"),
        "operation capability authorization_id",
    )
    ledger = external_harness_authorization_ledger(root, task_id, attempt_id)
    grant = _lookup_active_harness_grant_record(ledger, authorization_id)
    if grant.get("capability") != capability:
        raise OperationAuthorizationError(
            "persisted authorization capability does not match task contract"
        )
    return grant


def external_harness_authorization_ledger(
    root: Path,
    task_id: str,
    attempt_id: str,
) -> AuthorizationLedger:
    """Canonical ledger path for an external-harness grant."""
    task_id = _simple_id(task_id, "task id")
    attempt_id = _simple_id(attempt_id, "attempt id")
    if not root.is_absolute():
        raise OperationAuthorizationError("root must be an absolute path")
    ledger_path = (
        root / ".omx/framework/team/operation-authorizations"
        / task_id / f"{attempt_id}.jsonl"
    )
    return AuthorizationLedger(
        ledger_path,
        task_id=task_id,
        attempt_id=attempt_id,
    )


def _external_harness_derived_contract_hash(
    *,
    task_contract: dict[str, Any] | None,
    operation_contract: dict[str, Any],
    worker_id: str,
    task_id: str,
    attempt_id: str,
) -> str:
    """Match omx_visible_dispatch _canonical_dispatch_contract_hash exactly."""
    base = dict(task_contract) if task_contract else {}
    base.pop("operation_capability", None)
    return _canonical_json_hash(
        {
            "task_contract": base,
            "operation_contract": operation_contract,
            "worker_id": worker_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
        }
    )


_PINNED_INTERRUPT_RUNTIME: Final[dict[str, str]] = {
    "type": "opencode",
    "version": "1.18.29",
}


def admit_opencode_server_interrupt_canary(
    selector: Mapping[str, str],
) -> dict[str, Any]:
    """Re-validate the pinned manifest for one task-bound interrupt.

    Supplement admission plus the interrupt-only runtime pin: the already
    resolver-validated manifest must declare exactly runtime type
    ``opencode`` and version ``1.18.29``. The supplement contract itself is
    unchanged and keeps accepting any resolver-valid manifest.
    """

    checked = admit_opencode_server_supplement_canary(selector)
    manifest = checked.get("manifest")
    runtime = manifest.get("runtime") if isinstance(manifest, dict) else None
    if not isinstance(runtime, dict) or any(
        str(runtime.get(key) or "") != value
        for key, value in _PINNED_INTERRUPT_RUNTIME.items()
    ):
        raise OperationAuthorizationError(
            "interrupt requires the pinned opencode 1.18.29 runtime manifest; "
            f"got {runtime!r}"
        )
    return checked


SUPPLEMENT_SELECTION_FIELDS: Final[tuple[str, ...]] = ("agent", "model", "variant")
SUPPLEMENT_SELECTION_MODEL_FIELDS: Final[tuple[str, ...]] = (
    "providerID",
    "modelID",
)


def supplement_selection_payload(
    original_selection: Mapping[str, Any],
    selection_evidence: str,
) -> dict[str, Any]:
    """Validate and canonicalize the bound original agent/model/variant.

    The selection is host-supplied and hash-bound into the supplement grant;
    an empty variant means the original session carried no explicit variant,
    and the transport emits the backend's canonical ``default`` sentinel.
    """
    selection = _mapping(original_selection, "original selection")
    _exact_fields(
        selection,
        frozenset(SUPPLEMENT_SELECTION_FIELDS),
        "original selection",
    )
    agent = _string(selection.get("agent"), "original selection agent").strip()
    variant_value = selection.get("variant")
    if not isinstance(variant_value, str):
        raise OperationAuthorizationError(
            "original selection variant must be a string"
        )
    model = _mapping(selection.get("model"), "original selection model")
    _exact_fields(
        model,
        frozenset(SUPPLEMENT_SELECTION_MODEL_FIELDS),
        "original selection model",
    )
    canonical_model = {
        field: _string(
            model.get(field),
            f"original selection model {field}",
        ).strip()
        for field in SUPPLEMENT_SELECTION_MODEL_FIELDS
    }
    if not agent or not canonical_model["providerID"] or not canonical_model["modelID"]:
        raise OperationAuthorizationError(
            "original selection must name the agent, model provider, and model id"
        )
    return {
        "selection": {
            "agent": agent,
            "model": canonical_model,
            "variant": variant_value.strip(),
        },
        "selection_evidence": _string(
            selection_evidence,
            "selection evidence source",
        ),
    }


def _supplement_derived_contract_hash(
    *,
    task_contract: dict[str, Any] | None,
    operation_contract: dict[str, Any],
    worker_id: str,
    task_id: str,
    attempt_id: str,
    supplement_id: str,
    request_hash: str,
    original_selection: dict[str, str],
    selection_evidence: str,
    bindings_key: str = "supplement",
) -> str:
    """Bind one supplement request and its original selection into the grant hash."""
    base = dict(task_contract) if task_contract else {}
    base.pop("operation_capability", None)
    return _canonical_json_hash(
        {
            "task_contract": base,
            "operation_contract": operation_contract,
            "worker_id": worker_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            bindings_key: {
                (
                    "resume_id" if bindings_key == "resume" else "supplement_id"
                ): supplement_id,
                "request_hash": request_hash,
                "original_selection": dict(original_selection),
                "selection_evidence": selection_evidence,
            },
        }
    )


def prepare_external_harness_grant(
    *,
    root: Path,
    task_id: str,
    attempt_id: str,
    worker_id: str,
    task_contract: Mapping[str, Any] | None = None,
    event_id: str,
) -> dict[str, Any]:
    """Prepare one formal, versioned external-harness worker_dispatch grant.

    Trust boundary: the trusted host process is already authenticated with the
    direct user and is the authority boundary — not this library and not any
    caller-supplied data. This API therefore exposes only minimal host-supplied
    facts (root, task id, attempt id, worker id, optional base task_contract and
    an option-scoped event id) and derives every security-critical field inside:
    operation kind (worker_dispatch), worker scope, effect/gate union, canonical
    ledger path, authorization id, contract id/hash, and the full prepared
    task_contract. The reviewed principal (direct-user) and provenance are
    hard-coded; no caller principal, harness identity, capability, gate set, or
    ledger path is accepted. Callers must not embed operation_capability in the
    task_contract; the exact base contract is bound.

    Residual same-process trust limit: any Python process that can call this
    library in the same process as a legitimate host is as trusted as the host.
    This contract cannot defend against code running with equal authority inside
    the trusted process; it only guarantees that untrusted *caller payloads*
    cannot lower or extend the derived contract.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        raise OperationAuthorizationError("root must be an absolute path")
    task_id = _simple_id(task_id, "task id")
    attempt_id = _simple_id(attempt_id, "attempt id")
    worker_id = _string(worker_id, "worker id")
    base_contract: dict[str, Any] = {}
    if task_contract is not None:
        base_contract = dict(_mapping(task_contract, "task contract"))
        if "operation_capability" in base_contract:
            raise OperationAuthorizationError(
                "caller cannot embed operation capability"
            )
    event_id_text = _string(event_id, "external harness event id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", event_id_text):
        raise OperationAuthorizationError(
            "external harness event id is not a simple identifier"
        )
    event_id = event_id_text
    operation_kind = "worker_dispatch"
    authorization_id = f"grant-{task_id}-{attempt_id}-{event_id}"
    contract_id = f"contract-{task_id}-{attempt_id}-{event_id}"
    operation_contract = derive_operation_contract(
        operation_kind,
        base_contract,
    )
    contract_hash = _external_harness_derived_contract_hash(
        task_contract=base_contract,
        operation_contract=operation_contract,
        worker_id=worker_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    scope = {"kind": "worker", "id": worker_id}
    capability = {
        "schema_version": 1,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "authorization_id": authorization_id,
        "authorization_source": "authorization_ledger",
        "operation_kind": operation_kind,
        "scope": scope,
        "mutation_gates": list(
            operation_contract["required_mutation_gates"]
        ),
        "task_id": task_id,
        "attempt_id": attempt_id,
    }
    ledger = external_harness_authorization_ledger(
        root,
        task_id,
        attempt_id,
    )
    grant_record = {
        "schema_version": 1,
        "authorization_id": authorization_id,
        "principal": "direct-user",
        "decision": "grant",
        "task_id": task_id,
        "attempt_id": attempt_id,
        "contract_hash": contract_hash,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "provenance": _EXTERNAL_HARNESS_SOURCE,
        "capability": capability,
    }
    validation = validate_operation_capability(
        capability,
        operation_kind=operation_kind,
        target_scope=scope,
        required_mutation_gates=list(
            operation_contract["required_mutation_gates"]
        ),
        task_id=task_id,
        attempt_id=attempt_id,
    )
    prepared_task_contract: dict[str, Any] = {
        **base_contract,
        "operation_capability": validation,
    }
    canary_selector = extract_opencode_server_prompt_async_canary(base_contract)
    if canary_selector is not None:
        admit_opencode_server_prompt_async_canary(canary_selector)
    appended = ledger.append(grant_record)
    boundary = int(appended["sequence"])
    return {
        "schema_version": 1,
        "grant_kind": "external_harness_direct_user_grant",
        "authorization_id": authorization_id,
        "authorization_source": _EXTERNAL_HARNESS_SOURCE,
        "event_id": event_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "operation_kind": operation_kind,
        "ledger_sequence": boundary,
        "capability": validation,
        "prepared_task_contract": prepared_task_contract,
        "consumption_state": "active",
    }


def prepare_external_harness_supplement_grant(
    *,
    root: Path,
    task_id: str,
    attempt_id: str,
    worker_id: str,
    supplement_id: str,
    request_hash: str,
    original_selection: Mapping[str, Any],
    selection_evidence: str,
    base_task_contract: Mapping[str, Any] | None = None,
    event_id: str,
) -> dict[str, Any]:
    """Prepare one formal same-task supplement grant for the trusted host.

    Same trust boundary as ``prepare_external_harness_grant``: the already
    authenticated host supplies only minimal facts, and this library derives
    the operation kind (``worker_supplement``), worker scope, effect/gate
    union, canonical ledger path, authorization id, and the contract hash
    that binds this exact supplement id, request hash, and original
    agent/model/variant selection. A distinct authorization id namespace is
    used, so a consumed dispatch grant can never authorize a supplement and
    vice versa.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        raise OperationAuthorizationError("root must be an absolute path")
    task_id = _simple_id(task_id, "task id")
    attempt_id = _simple_id(attempt_id, "attempt id")
    worker_id = _string(worker_id, "worker id")
    supplement_id = _simple_id(supplement_id, "supplement id")
    request_hash = _sha256(request_hash, "supplement request hash")
    bound = supplement_selection_payload(original_selection, selection_evidence)
    base_contract: dict[str, Any] = {}
    if base_task_contract is not None:
        base_contract = dict(_mapping(base_task_contract, "task contract"))
        if "operation_capability" in base_contract:
            raise OperationAuthorizationError(
                "caller cannot embed operation capability"
            )
    event_id_text = _string(event_id, "external harness event id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", event_id_text):
        raise OperationAuthorizationError(
            "external harness event id is not a simple identifier"
        )
    operation_kind = "worker_supplement"
    authorization_id = (
        f"grant-{task_id}-{attempt_id}-supplement-{event_id_text}"
    )
    contract_id = f"contract-{task_id}-{attempt_id}-supplement-{event_id_text}"
    operation_contract = derive_operation_contract(
        operation_kind,
        base_contract,
    )
    contract_hash = _supplement_derived_contract_hash(
        task_contract=base_contract,
        operation_contract=operation_contract,
        worker_id=worker_id,
        task_id=task_id,
        attempt_id=attempt_id,
        supplement_id=supplement_id,
        request_hash=request_hash,
        original_selection=bound["selection"],
        selection_evidence=bound["selection_evidence"],
    )
    scope = {"kind": "worker", "id": worker_id}
    capability = {
        "schema_version": 1,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "authorization_id": authorization_id,
        "authorization_source": "authorization_ledger",
        "operation_kind": operation_kind,
        "scope": scope,
        "mutation_gates": list(
            operation_contract["required_mutation_gates"]
        ),
        "task_id": task_id,
        "attempt_id": attempt_id,
    }
    validation = validate_operation_capability(
        capability,
        operation_kind=operation_kind,
        target_scope=scope,
        required_mutation_gates=list(
            operation_contract["required_mutation_gates"]
        ),
        task_id=task_id,
        attempt_id=attempt_id,
    )
    canary_selector = extract_opencode_server_prompt_async_canary(base_contract)
    if canary_selector is not None:
        admit_opencode_server_supplement_canary(canary_selector)
    ledger = external_harness_authorization_ledger(
        root,
        task_id,
        attempt_id,
    )
    appended = ledger.append(
        {
            "schema_version": 1,
            "authorization_id": authorization_id,
            "principal": "direct-user",
            "decision": "grant",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "contract_hash": contract_hash,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "provenance": _EXTERNAL_HARNESS_SOURCE,
            "capability": validation,
        }
    )
    return {
        "schema_version": 1,
        "grant_kind": "external_harness_direct_user_supplement_grant",
        "authorization_id": authorization_id,
        "authorization_source": _EXTERNAL_HARNESS_SOURCE,
        "event_id": event_id_text,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "operation_kind": operation_kind,
        "ledger_sequence": int(appended["sequence"]),
        "capability": validation,
        "supplement_id": supplement_id,
        "request_hash": request_hash,
        "original_selection": dict(bound["selection"]),
        "selection_evidence": bound["selection_evidence"],
        "consumption_state": "active",
    }


def _interrupt_derived_contract_hash(
    *,
    task_contract: dict[str, Any] | None,
    operation_contract: dict[str, Any],
    worker_id: str,
    task_id: str,
    attempt_id: str,
    interrupt_id: str,
    request_hash: str,
    original_selection: dict[str, str],
    selection_evidence: str,
) -> str:
    """Bind one interrupt request and the task's original selection into the grant hash."""
    base = dict(task_contract) if task_contract else {}
    base.pop("operation_capability", None)
    return _canonical_json_hash(
        {
            "task_contract": base,
            "operation_contract": operation_contract,
            "worker_id": worker_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "interrupt": {
                "interrupt_id": interrupt_id,
                "request_hash": request_hash,
                "original_selection": dict(original_selection),
                "selection_evidence": selection_evidence,
            },
        }
    )


def prepare_external_harness_interrupt_grant(
    *,
    root: Path,
    task_id: str,
    attempt_id: str,
    worker_id: str,
    interrupt_id: str,
    request_hash: str,
    original_selection: Mapping[str, Any] | None = None,
    selection_evidence: str = "",
    base_task_contract: Mapping[str, Any] | None = None,
    event_id: str,
) -> dict[str, Any]:
    """Prepare one formal task-bound interrupt grant for the trusted host.

    Same trust boundary as the supplement grant: the already authenticated
    host supplies only minimal facts, and this library derives the operation
    kind (``worker_interrupt``), worker scope, effect/gate union, canonical
    ledger path, authorization id, and the contract hash that binds this
    exact interrupt id, request hash, and — when the task carries one — the
    task's bound agent/model/variant selection. The authorization id
    namespace is distinct, so a dispatch or supplement grant can never
    authorize an interrupt and vice versa.

    An interrupt without an original selection is abort-only: the bodyless
    session abort sends no prompt and no model facts, so default TUI tasks
    carry an empty selection binding instead of a refusal. The abort-only
    entry capability is judged live at the transport (the bound worker's
    ``/doc`` must declare the pinned abort schema and the exact fenced
    session must exist); the prompt_async supplement manifest and runtime
    version never gate it here.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        raise OperationAuthorizationError("root must be an absolute path")
    task_id = _simple_id(task_id, "task id")
    attempt_id = _simple_id(attempt_id, "attempt id")
    worker_id = _string(worker_id, "worker id")
    interrupt_id = _simple_id(interrupt_id, "interrupt id")
    request_hash = _sha256(request_hash, "interrupt request hash")
    if original_selection:
        bound = supplement_selection_payload(original_selection, selection_evidence)
    else:
        bound = {"selection": {}, "selection_evidence": ""}
    base_contract: dict[str, Any] = {}
    if base_task_contract is not None:
        base_contract = dict(_mapping(base_task_contract, "task contract"))
        if "operation_capability" in base_contract:
            raise OperationAuthorizationError(
                "caller cannot embed operation capability"
            )
    event_id_text = _string(event_id, "external harness event id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", event_id_text):
        raise OperationAuthorizationError(
            "external harness event id is not a simple identifier"
        )
    operation_kind = "worker_interrupt"
    authorization_id = (
        f"grant-{task_id}-{attempt_id}-interrupt-{event_id_text}"
    )
    contract_id = f"contract-{task_id}-{attempt_id}-interrupt-{event_id_text}"
    operation_contract = derive_operation_contract(
        operation_kind,
        base_contract,
    )
    contract_hash = _interrupt_derived_contract_hash(
        task_contract=base_contract,
        operation_contract=operation_contract,
        worker_id=worker_id,
        task_id=task_id,
        attempt_id=attempt_id,
        interrupt_id=interrupt_id,
        request_hash=request_hash,
        original_selection=bound["selection"],
        selection_evidence=bound["selection_evidence"],
    )
    scope = {"kind": "worker", "id": worker_id}
    capability = {
        "schema_version": 1,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "authorization_id": authorization_id,
        "authorization_source": "authorization_ledger",
        "operation_kind": operation_kind,
        "scope": scope,
        "mutation_gates": list(
            operation_contract["required_mutation_gates"]
        ),
        "task_id": task_id,
        "attempt_id": attempt_id,
    }
    validation = validate_operation_capability(
        capability,
        operation_kind=operation_kind,
        target_scope=scope,
        required_mutation_gates=list(
            operation_contract["required_mutation_gates"]
        ),
        task_id=task_id,
        attempt_id=attempt_id,
    )
    ledger = external_harness_authorization_ledger(
        root,
        task_id,
        attempt_id,
    )
    appended = ledger.append(
        {
            "schema_version": 1,
            "authorization_id": authorization_id,
            "principal": "direct-user",
            "decision": "grant",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "contract_hash": contract_hash,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "provenance": _EXTERNAL_HARNESS_SOURCE,
            "capability": validation,
        }
    )
    return {
        "schema_version": 1,
        "grant_kind": "external_harness_direct_user_interrupt_grant",
        "authorization_id": authorization_id,
        "authorization_source": _EXTERNAL_HARNESS_SOURCE,
        "event_id": event_id_text,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "operation_kind": operation_kind,
        "ledger_sequence": int(appended["sequence"]),
        "capability": validation,
        "interrupt_id": interrupt_id,
        "request_hash": request_hash,
        "original_selection": dict(bound["selection"]),
        "selection_evidence": bound["selection_evidence"],
        "consumption_state": "active",
    }


def prepare_external_harness_supplement_resume_grant(
    *,
    root: Path,
    task_id: str,
    attempt_id: str,
    worker_id: str,
    resume_id: str,
    request_hash: str,
    original_selection: Mapping[str, Any],
    selection_evidence: str,
    base_task_contract: Mapping[str, Any] | None = None,
    event_id: str,
) -> dict[str, Any]:
    """Prepare one same-task resume grant under the ``worker_supplement`` kind.

    The resume is a supplement-route message, so the operation kind stays
    ``worker_supplement`` and the ledger is the same. What makes it resume
    authority is the bound context: the request hash supplied by the trusted
    host must already bind intent, full identity/selection/text, the exact
    immutable interrupt receipt, and the canonical writer-verification
    record; this grant additionally binds resume id, request hash, selection
    and evidence under the ``resume`` key, so an ordinary supplement grant
    (bound under the ``supplement`` key) can never authorize a resume and
    vice versa. Admission re-uses the interrupt pin: the resolver-valid
    manifest must declare the supported existing-session opencode 1.18.29
    boundary.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        raise OperationAuthorizationError("root must be an absolute path")
    task_id = _simple_id(task_id, "task id")
    attempt_id = _simple_id(attempt_id, "attempt id")
    worker_id = _string(worker_id, "worker id")
    resume_id = _simple_id(resume_id, "resume id")
    request_hash = _sha256(request_hash, "resume request hash")
    bound = supplement_selection_payload(original_selection, selection_evidence)
    base_contract: dict[str, Any] = {}
    if base_task_contract is not None:
        base_contract = dict(_mapping(base_task_contract, "task contract"))
        if "operation_capability" in base_contract:
            raise OperationAuthorizationError(
                "caller cannot embed operation capability"
            )
    event_id_text = _string(event_id, "external harness event id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", event_id_text):
        raise OperationAuthorizationError(
            "external harness event id is not a simple identifier"
        )
    operation_kind = "worker_supplement"
    authorization_id = (
        f"grant-{task_id}-{attempt_id}-resume-{event_id_text}"
    )
    contract_id = f"contract-{task_id}-{attempt_id}-resume-{event_id_text}"
    operation_contract = derive_operation_contract(
        operation_kind,
        base_contract,
    )
    contract_hash = _supplement_derived_contract_hash(
        task_contract=base_contract,
        operation_contract=operation_contract,
        worker_id=worker_id,
        task_id=task_id,
        attempt_id=attempt_id,
        supplement_id=resume_id,
        request_hash=request_hash,
        original_selection=bound["selection"],
        selection_evidence=bound["selection_evidence"],
        bindings_key="resume",
    )
    scope = {"kind": "worker", "id": worker_id}
    capability = {
        "schema_version": 1,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "authorization_id": authorization_id,
        "authorization_source": "authorization_ledger",
        "operation_kind": operation_kind,
        "scope": scope,
        "mutation_gates": list(
            operation_contract["required_mutation_gates"]
        ),
        "task_id": task_id,
        "attempt_id": attempt_id,
    }
    validation = validate_operation_capability(
        capability,
        operation_kind=operation_kind,
        target_scope=scope,
        required_mutation_gates=list(
            operation_contract["required_mutation_gates"]
        ),
        task_id=task_id,
        attempt_id=attempt_id,
    )
    canary_selector = extract_opencode_server_prompt_async_canary(base_contract)
    if canary_selector is not None:
        admit_opencode_server_interrupt_canary(canary_selector)
    ledger = external_harness_authorization_ledger(
        root,
        task_id,
        attempt_id,
    )
    appended = ledger.append(
        {
            "schema_version": 1,
            "authorization_id": authorization_id,
            "principal": "direct-user",
            "decision": "grant",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "contract_hash": contract_hash,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "provenance": _EXTERNAL_HARNESS_SOURCE,
            "capability": validation,
        }
    )
    return {
        "schema_version": 1,
        "grant_kind": "external_harness_direct_user_supplement_resume_grant",
        "authorization_id": authorization_id,
        "authorization_source": _EXTERNAL_HARNESS_SOURCE,
        "event_id": event_id_text,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "contract_id": contract_id,
        "contract_hash": contract_hash,
        "operation_kind": operation_kind,
        "ledger_sequence": int(appended["sequence"]),
        "capability": validation,
        "resume_id": resume_id,
        "request_hash": request_hash,
        "original_selection": dict(bound["selection"]),
        "selection_evidence": bound["selection_evidence"],
        "consumption_state": "active",
    }


def _lookup_active_harness_grant_record(
    ledger: AuthorizationLedger,
    authorization_id: str,
) -> dict[str, Any]:
    records = ledger.records()
    active: dict[str, dict[str, Any]] = {}
    for record in records:
        match record["decision"]:
            case "grant":
                active[record["authorization_id"]] = record
            case "revoke":
                active.pop(record["revokes_authorization_id"], None)
            case unreachable:
                raise AssertionError(f"unexpected ledger action: {unreachable}")
    grant = active.get(authorization_id)
    if grant is None:
        raise OperationAuthorizationError("authorization is inactive")
    if str(grant.get("provenance") or "") != _EXTERNAL_HARNESS_SOURCE:
        raise OperationAuthorizationError("authorization is not an external harness grant")
    return grant


def _harness_capability_claim_path(
    root: Path,
    capability: dict[str, Any],
) -> Path:
    claim_id = _canonical_json_hash(capability)
    return (
        root
        / ".omx/framework/team/visible-runs/_dispatch-claims"
        / f"{claim_id.removeprefix('sha256:')}.json"
    )


def reconcile_external_harness_grant(
    *,
    root: Path,
    authorization_id: str,
    task_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Deterministic pre-task crash reconciliation for one harness grant.

    Evidence is derived from runtime files only, never from caller-disclosed
    booleans. Three evidence sources are inspected with lstat/no-follow
    semantics: the exact capability dispatch-claim file (any lstat-observed
    final path is evidence), candidate visible-run task.json records for this
    task_id matched on exact attempt_id plus authorization_id in
    operation_contract.authorization, and canonical operation-receipts
    records matched on task_id/attempt_id/authorization_id. Historical task
    entries that cannot prove all three identifiers are ignored; the exact
    capability claim remains the authoritative guard for incomplete creation.
    """
    if not isinstance(root, Path) or not root.is_absolute():
        raise OperationAuthorizationError("root must be an absolute path")
    authorization_id = _string(authorization_id, "authorization id")
    task_id = _simple_id(task_id, "task id")
    attempt_id = _simple_id(attempt_id, "attempt id")
    ledger = external_harness_authorization_ledger(
        root,
        task_id,
        attempt_id,
    )
    grant_record = _lookup_active_harness_grant_record(
        ledger,
        authorization_id,
    )
    capability = grant_record["capability"]
    reason = _harness_claim_observation(
        root,
        _harness_capability_claim_path(root, capability),
    )
    if not reason:
        reason = _harness_task_evidence_observation(
            root,
            task_id,
            attempt_id,
            authorization_id,
        )
    if not reason:
        reason = _harness_receipt_evidence_observation(
            root,
            task_id,
            attempt_id,
            authorization_id,
        )
    if reason:
        return {
            "schema_version": 1,
            "authorization_id": authorization_id,
            "decision": "reconcile_needed",
            "consumption_state": "active",
            "reconcile_reason": reason,
        }
    revoke_record = ledger.consume_authorization(
        authorization_id,
        principal="direct-user",
        provenance="external-harness-reconcile",
    )
    if revoke_record is None:
        raise OperationAuthorizationError("authorization revoke reuse is forbidden")
    return {
        "schema_version": 1,
        "authorization_id": authorization_id,
        "decision": "revoked",
        "consumption_state": "revoked",
        "reconcile_reason": "no dispatch claim, task, or receipt evidence",
    }


def _directory_chain_observation(root: Path, relative: Path) -> str:
    """Root-to-leaf lstat walk of a directory chain under root.

    ``relative`` may be an absolute path under root or a relative chain. Returns
    '' when a component is definitely absent (no evidence), 'safe' when every
    component is a non-symlink directory, or a reason string for an
    unsafe/unreadable component (reconcile-needed). The walk stops at the
    first absent component, so a missing deep child never masks a symlinked
    or unsafe ancestor.
    """
    if relative.is_absolute():
        try:
            parts = relative.relative_to(root).parts
        except ValueError:
            return "path escapes root"
    else:
        parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        if not parts:
            return "safe"
        return "path escapes root"
    current = root
    for part in parts:
        current = current / part
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError:
            return ""
        except OSError:
            return f"{part} unreadable"
        if os.path.islink(current) or not stat.S_ISDIR(entry_stat.st_mode):
            return f"{part} unsafe"
    return "safe"


def _harness_claim_observation(root: Path, claim_path: Path) -> str:
    """Evidence observation for the exact dispatch-claim path.

    No content is read: an lstat-observed final path is already evidence. The
    parent chain is verified root-to-leaf (_directory_chain_observation) so a
    FileNotFoundError behind a symlinked ancestor is never treated as absence.
    """
    chain = _directory_chain_observation(
        root,
        claim_path.parent,
    )
    if chain != "safe":
        return chain
    try:
        file_stat = os.lstat(claim_path)
    except FileNotFoundError:
        return ""
    except OSError:
        return "claim path unsafe"
    if os.path.islink(claim_path) or not stat.S_ISREG(file_stat.st_mode):
        return "dispatch claim unsafe"
    return "dispatch claim present"


def _json_record_observation(
    path: Path,
    limit: int,
    label: str,
) -> tuple[dict[str, Any] | None, str]:
    """Read one JSON object no-follow; (record, "") on readable, (None, reason)
    on unsafe/unreadable/malformed, (None, "") on definite absence."""
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return None, ""
    except OSError:
        return None, f"{label} unreadable"
    if os.path.islink(path) or not stat.S_ISREG(file_stat.st_mode):
        return None, f"{label} unsafe"
    try:
        raw = _read_nofollow_bytes(path, limit)
    except (OSError, ValueError):
        return None, f"{label} observation uncertainty"
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, f"{label} observation uncertainty"
    if not isinstance(record, dict):
        return None, f"{label} observation uncertainty"
    return record, ""


def _harness_task_evidence_observation(
    root: Path,
    task_id: str,
    attempt_id: str,
    authorization_id: str,
) -> str:
    """Scan visible-run task.json records for an exact task/attempt/auth match."""
    runs_relative = Path(".omx/framework/team/visible-runs")
    chain = _directory_chain_observation(root, runs_relative)
    if chain != "safe":
        return chain
    try:
        runs = sorted((root / runs_relative).iterdir())
    except OSError:
        return "visible-runs unreadable"
    for run in runs:
        if not str(run.name).startswith("visible-run-"):
            continue
        task_chain = _directory_chain_observation(
            root,
            runs_relative / run.name / "tasks" / task_id,
        )
        if task_chain != "safe":
            continue
        task_dir = (root / runs_relative / run.name / "tasks" / task_id)
        record, observation = _json_record_observation(
            task_dir / "task.json",
            4 * 1024 * 1024,
            "task record",
        )
        if observation or record is None:
            continue
        if str(record.get("id") or "") != task_id:
            continue
        if str(record.get("attempt_id") or "") != attempt_id:
            continue
        operation_contract = record.get("operation_contract")
        authorization = (
            operation_contract.get("authorization")
            if isinstance(operation_contract, dict)
            else None
        )
        if (
            isinstance(authorization, dict)
            and str(authorization.get("authorization_id") or "")
            == authorization_id
        ):
            return "exact task record matches harness grant"
    return ""


def _harness_receipt_evidence_observation(
    root: Path,
    task_id: str,
    attempt_id: str,
    authorization_id: str,
) -> str:
    """Scan canonical operation-receipts for an exact task/attempt/auth match.

    Production receipts live at
    root/.omx/framework/team/operation-receipts/<run_id>/<task_id>.json
    (see omx_visible_dispatch._record_operation_receipt).
    """
    receipts_relative = Path(".omx/framework/team/operation-receipts")
    chain = _directory_chain_observation(root, receipts_relative)
    if chain != "safe":
        return chain
    try:
        runs = sorted((root / receipts_relative).iterdir())
    except OSError:
        return "operation-receipts unreadable"
    for run in runs:
        if not str(run.name).startswith("visible-run-"):
            continue
        run_chain = _directory_chain_observation(
            root,
            receipts_relative / run.name,
        )
        if run_chain != "safe":
            if run_chain:
                return run_chain
            continue
        record, observation = _json_record_observation(
            run / f"{task_id}.json",
            4 * 1024 * 1024,
            "receipt record",
        )
        if observation:
            return observation
        if record is None:
            continue
        if (
            str(record.get("task_id") or "") == task_id
            and str(record.get("attempt_id") or "") == attempt_id
            and str(record.get("authorization_id") or "") == authorization_id
        ):
            return "exact operation receipt matches harness grant"
    return ""


def _read_nofollow_bytes(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise ValueError("file exceeds read limit")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
