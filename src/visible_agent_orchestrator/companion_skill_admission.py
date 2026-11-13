"""Fail-closed user-global Companion Skill lifecycle admission."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Final, Iterator, Mapping


class AdmissionError(RuntimeError):
    """Durable lifecycle evidence or admission policy is unsafe."""


class LifecycleState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    OPEN = "OPEN"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    INVALID = "INVALID"


_MAX_BYTES: Final = 1024 * 1024
_SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RUNTIME: Final = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_TIME: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_HELD: ContextVar[frozenset[Path]] = ContextVar("companion_admission_held", default=frozenset())


@dataclass
class _GateHolds:
    """Mutable lock descriptors owned by the enclosing admission_gate turn."""

    turnstile: int
    resource: int
    deferred: bool = False


@dataclass(frozen=True, slots=True)
class _ActiveSharedAdmission:
    gate: "AdmissionGate"
    command: str


_ACTIVE_SHARED: ContextVar[_ActiveSharedAdmission | None] = ContextVar(
    "companion_admission_active_shared", default=None
)
_READONLY_WAIT_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "omx_visible_dispatch:wait",
        "omx_visible_dispatch:run-one",
        "omx_visible_dispatch:run-one-native",
        "omx_visible_orchestrate:run",
    }
)
_COMMAND: Final = re.compile(r"omx_[a-z0-9_]+:[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)?\Z")
TRUSTED_RUNTIME_IDENTITY: Final = "companion-skill-admission-v1"
TRUSTED_CONTRACT_GENERATION: Final = 1
_ANCHOR_NAME: Final = "bootstrap.anchor"
_ANCHOR_BYTES: Final = b"companion-skill-admission-v1\n"


def control_root() -> Path:
    """Derive the live root from passwd data for the effective uid, never HOME."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".local" / "state" / "visible-agent-orchestrator" / "companion-skills"


def held_exclusive_admission_root() -> Path:
    """Return the sole active exclusive lifecycle root for an internal handler."""
    roots = _HELD.get()
    if len(roots) != 1:
        raise AdmissionError("exactly one exclusive admission root is required")
    return next(iter(roots))


def _root(value: Path | None) -> Path:
    candidate = control_root() if value is None else value
    if not candidate.is_absolute():
        raise AdmissionError("control root must be absolute")
    return candidate


def _directory(path: Path, create: bool) -> int | None:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                os.mkdir(part, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise AdmissionError("control directory is not owner-controlled")
        return descriptor
    except (OSError, AdmissionError) as exc:
        os.close(descriptor)
        if isinstance(exc, AdmissionError):
            raise
        raise AdmissionError("control path is unsafe") from exc


def _regular(root: Path, name: str, create: bool) -> int | None:
    parent = _directory(root, create)
    if parent is None:
        return None
    try:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                return None
            descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            os.fsync(parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(descriptor)
            raise AdmissionError(f"{name} is not an owner-controlled regular file")
        return descriptor
    except OSError as exc:
        raise AdmissionError(f"{name} is unsafe") from exc
    finally:
        os.close(parent)


def _bounded_read(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    data = bytearray()
    while chunk := os.read(descriptor, min(65536, _MAX_BYTES + 1 - len(data))):
        data.extend(chunk)
        if len(data) > _MAX_BYTES:
            raise AdmissionError("state exceeds maximum bytes")
    return bytes(data)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_time(value: object) -> bool:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).isoformat(timespec="microseconds").replace("+00:00", "Z") == value
    except ValueError:
        return False


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _base(record: Mapping[str, object], sequence: int) -> bool:
    rollout_ok = "rollout_id" not in record or (type(record.get("rollout_id")) is int and int(record["rollout_id"]) >= 0)
    return record.get("protocol_version") == 1 and record.get("sequence") == sequence and _valid_time(record.get("timestamp")) and _identifier(record.get("transaction_id")) and _SHA256.fullmatch(str(record.get("registry_sha256"))) is not None and _RUNTIME.fullmatch(str(record.get("runtime_identity"))) is not None and type(record.get("contract_generation")) is int and int(record["contract_generation"]) >= 1 and rollout_ok


def _valid_record(item: Mapping[str, object], sequence: int) -> bool:
    kind = item.get("event")
    base_fields = {"event", "protocol_version", "sequence", "timestamp", "transaction_id", "registry_sha256", "runtime_identity", "contract_generation"}
    bootstrap = base_fields | ({"rollout_id"} if "rollout_id" in item else set())
    release = bootstrap | {"manifest_sha256", "old_release_id", "new_release_id"}
    expected = bootstrap if kind == "BOOTSTRAPPED" else release if kind in {"PREPARED", "RUNTIME_PREPARED", "ROLLBACK_PREPARED", "ACTIVATED", "RUNTIME_ACTIVATED", "ROLLED_BACK"} else set()
    return set(item) == expected and _base(item, sequence) and (kind == "BOOTSTRAPPED" or (_SHA256.fullmatch(str(item.get("manifest_sha256"))) is not None and _identifier(item.get("old_release_id")) and _identifier(item.get("new_release_id"))))


def _valid_candidate(item: Mapping[str, object]) -> bool:
    probe = {**item, "sequence": 1, "timestamp": "2000-01-01T00:00:00.000000Z", "rollout_id": 0}
    return _valid_record(probe, 1)


def _records(root: Path) -> list[dict[str, object]] | None:
    descriptor = _regular(root, "journal.jsonl", False)
    if descriptor is None:
        return None
    try:
        raw = _bounded_read(descriptor)
    finally:
        os.close(descriptor)
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise AdmissionError("journal has torn tail")
    results: list[dict[str, object]] = []
    explicit_seen = False
    for sequence, line in enumerate(raw.splitlines(), 1):
        try:
            item = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdmissionError("journal malformed") from exc
        if not isinstance(item, dict):
            raise AdmissionError("journal event invalid")
        if json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() != line:
            raise AdmissionError("journal event invalid")
        has_rollout = "rollout_id" in item
        if has_rollout:
            explicit_seen = True
        elif explicit_seen:
            raise AdmissionError("journal missing rollout_id after explicit rollout records")
        if not _base(item, sequence):
            raise AdmissionError("journal event invalid")
        if not _valid_record(item, sequence):
            raise AdmissionError("journal schema invalid")
        results.append(item)

    derived_rollout_id = 0
    for record in results:
        if "rollout_id" not in record:
            if record["event"] == "BOOTSTRAPPED":
                derived_rollout_id += 1
            record["rollout_id"] = derived_rollout_id
        else:
            if record["event"] == "BOOTSTRAPPED":
                if record["rollout_id"] <= derived_rollout_id:
                    raise AdmissionError("journal bootstrap rollout identity stale or downgraded")
                derived_rollout_id = record["rollout_id"]
            elif record["rollout_id"] != derived_rollout_id:
                raise AdmissionError("journal release rollout identity conflicts")
    return results


def _anchor_state(root: Path) -> bool | None:
    descriptor = _regular(root, _ANCHOR_NAME, False)
    if descriptor is None:
        return None
    try:
        if _bounded_read(descriptor) != _ANCHOR_BYTES:
            raise AdmissionError("bootstrap anchor is invalid")
        return True
    finally:
        os.close(descriptor)


def _project_boundary(records: list[dict[str, object]]) -> tuple[LifecycleState, tuple[object, object, object, int]]:
    """Project exact event pairs and the latest valid bootstrap boundary.

    Historical completed transactions must match the identity active when they
    occurred; each authorized bootstrap rollover advances the monotonic rollout
    identity that later transactions must match.
    """
    if not records or records[0].get("event") != "BOOTSTRAPPED":
        raise AdmissionError("journal missing bootstrap")
    boundary = tuple(records[0][key] for key in ("registry_sha256", "runtime_identity", "contract_generation", "rollout_id"))
    seen_identities = {boundary}
    pending: dict[str, object] | None = None
    for index, record in enumerate(records):
        if index == 0:
            continue
        kind = record["event"]
        if kind == "BOOTSTRAPPED":
            if pending is not None:
                raise AdmissionError("journal bootstrap during pending transaction")
            candidate = tuple(record[key] for key in ("registry_sha256", "runtime_identity", "contract_generation", "rollout_id"))
            if candidate in seen_identities:
                raise AdmissionError("journal bootstrap identity reused")
            if record["rollout_id"] <= boundary[3]:
                raise AdmissionError("journal bootstrap rollout identity stale or downgraded")
            if tuple(record[key] for key in ("registry_sha256", "runtime_identity", "contract_generation")) == boundary[:3]:
                raise AdmissionError("journal bootstrap identity repeated")
            seen_identities.add(candidate)
            boundary = candidate
            continue
        if tuple(record[key] for key in ("registry_sha256", "runtime_identity", "contract_generation", "rollout_id")) != boundary:
            raise AdmissionError("journal identity conflicts")
        if kind in {"PREPARED", "RUNTIME_PREPARED", "ROLLBACK_PREPARED"}:
            if pending is not None:
                raise AdmissionError("journal has concurrent transactions")
            pending = record
        elif kind in {"ACTIVATED", "RUNTIME_ACTIVATED", "ROLLED_BACK"}:
            if pending is None:
                raise AdmissionError("journal terminal without pending transaction")
            if pending["event"] == "PREPARED":
                expected_terminal = "ACTIVATED"
            elif pending["event"] == "RUNTIME_PREPARED":
                expected_terminal = "RUNTIME_ACTIVATED"
            else:
                expected_terminal = "ROLLED_BACK"
            keys = ("transaction_id", "registry_sha256", "runtime_identity", "contract_generation", "manifest_sha256", "old_release_id", "new_release_id", "rollout_id")
            if kind != expected_terminal or any(record[key] != pending[key] for key in keys):
                raise AdmissionError("journal terminal identity conflicts")
            pending = None
        else:
            raise AdmissionError("journal event direction invalid")
    return LifecycleState.RECOVERY_REQUIRED if pending else LifecycleState.OPEN, boundary


def _project(records: list[dict[str, object]]) -> LifecycleState:
    return _project_boundary(records)[0]


def lifecycle_state(root: Path | None = None) -> LifecycleState:
    """Project exact event pairs; corrupt, stale, or conflicting evidence is invalid."""
    try:
        candidate = _root(root)
        records = _records(candidate)
        anchor = _anchor_state(candidate)
        if records is None and anchor is None:
            return LifecycleState.UNINITIALIZED
        if records is None or anchor is None:
            return LifecycleState.INVALID
        return _project(records)
    except AdmissionError:
        return LifecycleState.INVALID


def terminal_release(root: Path) -> Mapping[str, object] | None:
    candidate = _root(root)
    if candidate not in _HELD.get():
        raise AdmissionError("exclusive admission is required for terminal release")
    records = _records(candidate)
    if records is None:
        return None
    state = _project(records)
    if state is not LifecycleState.OPEN:
        raise AdmissionError("terminal release requires OPEN admission")
    terminal = next(
        (
            record
            for record in reversed(records)
            if record["event"] in {"ACTIVATED", "ROLLED_BACK"}
        ),
        None,
    )
    return terminal


def pending_transaction(root: Path) -> Mapping[str, object]:
    """Return the single durable PREPARED or ROLLBACK_PREPARED record.

    Reconcile may only finish this exact transaction; it never re-derives
    authorization or closure from physical pointers or mutable task prose.
    """
    candidate = _root(root)
    if candidate not in _HELD.get():
        raise AdmissionError("exclusive admission is required for pending transaction")
    records = _records(candidate)
    if records is None:
        raise AdmissionError("pending transaction requires an existing journal")
    state = _project(records)
    if state is not LifecycleState.RECOVERY_REQUIRED:
        raise AdmissionError("pending transaction requires RECOVERY_REQUIRED")
    return next(
        record
        for record in reversed(records)
        if record["event"] in {"PREPARED", "RUNTIME_PREPARED", "ROLLBACK_PREPARED"}
    )


def _append(root: Path, record: dict[str, object]) -> None:
    _write_journal(root, [record])


def _write_journal(root: Path, new_records: list[dict[str, object]]) -> None:
    """Atomically append durable records by rewriting the whole journal image."""
    if not new_records:
        return
    for record in new_records:
        if not _valid_candidate(record):
            raise AdmissionError("candidate event invalid")
    descriptor = _regular(root, "journal.jsonl", False)
    existing_raw = b""
    existing: list[dict[str, object]] = []
    try:
        if descriptor is not None:
            existing_raw = _bounded_read(descriptor)
            if existing_raw and not existing_raw.endswith(b"\n"):
                raise AdmissionError("journal has torn tail")
            existing = _records(root) or []
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if existing:
        _project_boundary(existing)
    next_rollout_id = max((r["rollout_id"] for r in existing if "rollout_id" in r), default=0) + 1
    boundary: tuple[object, object, object, int] | None = None
    if existing:
        boundary = tuple(existing[-1][key] for key in ("registry_sha256", "runtime_identity", "contract_generation", "rollout_id"))
    candidates: list[dict[str, object]] = []
    for offset, record in enumerate(new_records):
        candidate = {**record, "sequence": len(existing) + offset + 1, "timestamp": _timestamp()}
        if candidate["event"] == "BOOTSTRAPPED":
            candidate["rollout_id"] = next_rollout_id
            next_rollout_id += 1
        else:
            if boundary is None:
                raise AdmissionError("candidate release without bootstrap boundary")
            candidate["rollout_id"] = boundary[3]
        if not _valid_record(candidate, len(existing) + offset + 1):
            raise AdmissionError("candidate event invalid")
        candidates.append(candidate)
        if candidate["event"] == "BOOTSTRAPPED":
            boundary = tuple(candidate[key] for key in ("registry_sha256", "runtime_identity", "contract_generation", "rollout_id"))
    _project_boundary([*existing, *candidates])
    image = existing_raw + b"".join(
        json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        for candidate in candidates
    )
    journal_path = root / "journal.jsonl"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix="journal.jsonl.", suffix=".tmp", delete=False, mode="wb") as temp:
            temp_path = Path(temp.name)
            temp.write(image)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, journal_path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    parent = _directory(root, False)
    if parent is not None:
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def _create_anchor(root: Path) -> None:
    parent = _directory(root, True)
    assert parent is not None
    try:
        try:
            descriptor = os.open(
                _ANCHOR_NAME,
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError as exc:
            raise AdmissionError("bootstrap anchor already exists")
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise AdmissionError("bootstrap anchor is unsafe")
            view = memoryview(_ANCHOR_BYTES)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise AdmissionError("bootstrap anchor write failed")
                view = view[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.fsync(parent)
        os.close(parent)


@dataclass(frozen=True, slots=True)
class AdmissionGate:
    root: Path
    exclusive: bool
    holds: "_GateHolds | None" = None

    def current_identity(self) -> dict[str, object]:
        if (
            not self.exclusive
            or self.root not in _HELD.get()
            or lifecycle_state(self.root) is not LifecycleState.OPEN
        ):
            raise AdmissionError("current identity requires exclusive OPEN admission")
        _state, boundary = _project_boundary(_records(self.root) or [])
        return dict(
            zip(
                (
                    "registry_sha256",
                    "runtime_identity",
                    "contract_generation",
                    "rollout_id",
                ),
                boundary,
                strict=True,
            )
        )

    def prepare_identity_transition(
        self,
        *,
        transaction_id: str,
        manifest_sha256: str,
        old_release_id: str,
        new_release_id: str,
    ) -> None:
        identity = self.current_identity()
        _append(
            self.root,
            {
                "event": "RUNTIME_PREPARED",
                "protocol_version": 1,
                "transaction_id": transaction_id,
                "registry_sha256": identity["registry_sha256"],
                "runtime_identity": identity["runtime_identity"],
                "contract_generation": identity["contract_generation"],
                "manifest_sha256": manifest_sha256,
                "old_release_id": old_release_id,
                "new_release_id": new_release_id,
            },
        )

    def finish_identity_transition(
        self,
        *,
        activated: bool,
        transaction_id: str,
        registry_sha256: str,
        runtime_identity: str,
        contract_generation: int,
        manifest_sha256: str,
        old_release_id: str,
        new_release_id: str,
    ) -> None:
        if (
            not self.exclusive
            or self.root not in _HELD.get()
            or lifecycle_state(self.root)
            is not LifecycleState.RECOVERY_REQUIRED
        ):
            raise AdmissionError(
                "identity transition terminal requires exclusive RECOVERY_REQUIRED admission"
            )
        pending = pending_transaction(self.root)
        if not activated:
            raise AdmissionError(
                "identity transition rollback remains recovery-required"
            )
        terminal = {
            "event": "RUNTIME_ACTIVATED",
            "protocol_version": 1,
            **{
                key: pending[key]
                for key in (
                    "transaction_id",
                    "registry_sha256",
                    "runtime_identity",
                    "contract_generation",
                    "manifest_sha256",
                    "old_release_id",
                    "new_release_id",
                )
            },
        }
        expected = {
            "transaction_id": transaction_id,
            "manifest_sha256": manifest_sha256,
            "old_release_id": old_release_id,
            "new_release_id": new_release_id,
        }
        if any(pending[key] != value for key, value in expected.items()):
            raise AdmissionError("identity transition terminal conflicts")
        records = [
            terminal,
            {
                "event": "BOOTSTRAPPED",
                "protocol_version": 1,
                "transaction_id": f"bootstrap-{transaction_id}",
                "registry_sha256": registry_sha256,
                "runtime_identity": runtime_identity,
                "contract_generation": contract_generation,
            },
        ]
        _write_journal(self.root, records)

    def prepare(self, *, transaction_id: str, registry_sha256: str, runtime_identity: str, contract_generation: int, manifest_sha256: str, old_release_id: str, new_release_id: str) -> None:
        self._prepare("PREPARED", transaction_id, registry_sha256, runtime_identity, contract_generation, manifest_sha256, old_release_id, new_release_id)

    def prepare_rollback(self, *, transaction_id: str, registry_sha256: str, runtime_identity: str, contract_generation: int, manifest_sha256: str, old_release_id: str, new_release_id: str) -> None:
        self._prepare("ROLLBACK_PREPARED", transaction_id, registry_sha256, runtime_identity, contract_generation, manifest_sha256, old_release_id, new_release_id)

    def finish_activation(self, *, transaction_id: str, registry_sha256: str, runtime_identity: str, contract_generation: int, manifest_sha256: str, old_release_id: str, new_release_id: str) -> None:
        self._finish("ACTIVATED", transaction_id, registry_sha256, runtime_identity, contract_generation, manifest_sha256, old_release_id, new_release_id)

    def finish_rollback(self, *, transaction_id: str, registry_sha256: str, runtime_identity: str, contract_generation: int, manifest_sha256: str, old_release_id: str, new_release_id: str) -> None:
        self._finish("ROLLED_BACK", transaction_id, registry_sha256, runtime_identity, contract_generation, manifest_sha256, old_release_id, new_release_id)

    def _prepare(self, event: str, transaction_id: str, registry_sha256: str, runtime_identity: str, contract_generation: int, manifest_sha256: str, old_release_id: str, new_release_id: str) -> None:
        if not self.exclusive or self.root not in _HELD.get() or lifecycle_state(self.root) is not LifecycleState.OPEN:
            raise AdmissionError("prepare requires exclusive OPEN admission")
        records = _records(self.root) or []
        _state, boundary = _project_boundary(records)
        release_record = {
            "event": event,
            "protocol_version": 1,
            "transaction_id": transaction_id,
            "registry_sha256": registry_sha256,
            "runtime_identity": runtime_identity,
            "contract_generation": contract_generation,
            "manifest_sha256": manifest_sha256,
            "old_release_id": old_release_id,
            "new_release_id": new_release_id,
        }
        if boundary[:3] == (registry_sha256, runtime_identity, contract_generation):
            _append(self.root, {**release_record, "rollout_id": boundary[3]})
        else:
            bootstrap_record = {
                "event": "BOOTSTRAPPED",
                "protocol_version": 1,
                "transaction_id": f"bootstrap-{transaction_id}",
                "registry_sha256": registry_sha256,
                "runtime_identity": runtime_identity,
                "contract_generation": contract_generation,
            }
            _write_journal(self.root, [bootstrap_record, release_record])

    def _finish(self, event: str, transaction_id: str, registry_sha256: str, runtime_identity: str, contract_generation: int, manifest_sha256: str, old_release_id: str, new_release_id: str) -> None:
        if not self.exclusive or self.root not in _HELD.get() or lifecycle_state(self.root) is not LifecycleState.RECOVERY_REQUIRED:
            raise AdmissionError("terminal event requires exclusive RECOVERY_REQUIRED admission")
        _append(self.root, {"event": event, "protocol_version": 1, "transaction_id": transaction_id, "registry_sha256": registry_sha256, "runtime_identity": runtime_identity, "contract_generation": contract_generation, "manifest_sha256": manifest_sha256, "old_release_id": old_release_id, "new_release_id": new_release_id})


@contextmanager
def admission_gate(
    root: Path | None = None,
    *,
    exclusive: bool,
    bypass_waiting_writer: bool = False,
) -> Iterator[AdmissionGate]:
    """Use a writer turnstile and resource lock; exclusive contention fails busy at either lock."""
    candidate = _root(root)
    turnstile = _regular(candidate, "turnstile.lock", True)
    assert turnstile is not None
    try:
        resource = _regular(candidate, "resource.lock", True)
    except (AdmissionError, OSError):
        os.close(turnstile)
        raise
    assert resource is not None
    held = False
    try:
        if not bypass_waiting_writer:
            try:
                fcntl.flock(turnstile, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_EX)
            except OSError as exc:
                if not exclusive or exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise AdmissionError("exclusive admission is busy at the writer turnstile") from exc
        try:
            fcntl.flock(resource, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except OSError as exc:
            if not exclusive or exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise AdmissionError("exclusive admission is busy at the protected resource") from exc
        held = True
        if not bypass_waiting_writer:
            fcntl.flock(turnstile, fcntl.LOCK_UN)
        if not exclusive and lifecycle_state(candidate) is not LifecycleState.OPEN:
            raise AdmissionError("shared admission requires OPEN; recovery is required")
        token = _HELD.set(_HELD.get() | {candidate}) if exclusive else None
        try:
            yield AdmissionGate(candidate, exclusive, _GateHolds(turnstile, resource))
        finally:
            if token is not None:
                _HELD.reset(token)
    finally:
        if held:
            fcntl.flock(resource, fcntl.LOCK_UN)
        os.close(resource)
        os.close(turnstile)


def bootstrap(root: Path | None = None, *, registry_sha256: str, runtime_identity: str, contract_generation: int) -> None:
    candidate = _root(root)
    if candidate not in _HELD.get():
        raise AdmissionError("exclusive admission is required for bootstrap")
    state = lifecycle_state(candidate)
    if state is LifecycleState.UNINITIALIZED:
        _create_anchor(candidate)
        _append(candidate, {"event": "BOOTSTRAPPED", "protocol_version": 1, "transaction_id": "bootstrap-v1", "registry_sha256": registry_sha256, "runtime_identity": runtime_identity, "contract_generation": contract_generation})
        return
    if state is not LifecycleState.OPEN:
        raise AdmissionError(f"bootstrap requires UNINITIALIZED or OPEN, got {state}")
    _, boundary = _project_boundary(_records(candidate) or [])
    if boundary[:3] != (registry_sha256, runtime_identity, contract_generation):
        raise AdmissionError("bootstrap identity does not match current admission contract")


def _registry_bytes(path: Path) -> bytes:
    parts = path.parent.parts
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise AdmissionError("mutation registry unsafe")
            return _bounded_read(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AdmissionError("mutation registry unsafe") from exc
    finally:
        os.close(parent)


def mutation_policy(command: str) -> str:
    path = Path(__file__).resolve().parents[2] / "contracts" / "companion-skill-mutation-surfaces.json"
    try:
        raw = _registry_bytes(path)
    except AdmissionError:
        raise
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AdmissionError) as exc:
        raise AdmissionError("mutation registry invalid") from exc
    commands = payload.get("commands") if isinstance(payload, Mapping) and set(payload) == {"schema_version", "commands"} and type(payload.get("schema_version")) is int and payload.get("schema_version") == 1 else None
    allowed = {"read-only", "shared-admission", "exclusive-admission", "recovery-only"}
    if not isinstance(commands, dict) or not commands or not all(isinstance(key, str) and _COMMAND.fullmatch(key) and value in allowed for key, value in commands.items()):
        raise AdmissionError("mutation registry schema invalid")
    return str(commands[command]) if command in commands else (_ for _ in ()).throw(AdmissionError("undeclared mutation surface"))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AdmissionError("mutation registry contains duplicate key")
        result[key] = value
    return result


def mutation_registry_sha256() -> str:
    path = Path(__file__).resolve().parents[2] / "contracts" / "companion-skill-mutation-surfaces.json"
    return "sha256:" + hashlib.sha256(_registry_bytes(path)).hexdigest()


def trusted_bootstrap_identity_matches(root: Path) -> bool:
    records = _records(root)
    if not records:
        return False
    _, boundary = _project_boundary(records)
    return (
        boundary[0] == mutation_registry_sha256()
        and boundary[1] == TRUSTED_RUNTIME_IDENTITY
        and boundary[2] == TRUSTED_CONTRACT_GENERATION
    )


def resolved_command_identity(command: str, *, update_registry: bool = False) -> str:
    """Resolve the sole reviewed parsed-argument mutation variant."""
    if update_registry:
        if command != "omx_opencode_tui_adapter:verify-visible":
            raise AdmissionError("update-registry variant is not valid for command")
        return f"{command}:update-registry"
    return command


def invoke_cli_handler(
    binary: str,
    args: object,
    handler: Callable[[object], int],
    *,
    admission_root: Path | None = None,
) -> int:
    """Run a parsed CLI handler under the fixed registry policy when required."""
    command = getattr(args, "command", None)
    if not isinstance(command, str):
        raise AdmissionError("parsed command identity is missing")
    identity = resolved_command_identity(
        f"{binary}:{command}",
        update_registry=binary == "omx_opencode_tui_adapter" and command == "verify-visible" and getattr(args, "update_registry", False) is True,
    )
    policy = mutation_policy(identity)
    if policy == "read-only":
        return int(handler(args))
    with _admitted_policy(
        identity,
        policy,
        admission_root,
        allow_identity_transition=(
            identity == "omx_install_sync:apply"
            and getattr(args, "publish_runtime_identity", False) is True
        ),
    ):
        return int(handler(args))


@contextmanager
def admitted_command(command: str, *, root: Path | None = None) -> Iterator[AdmissionGate]:
    policy = mutation_policy(command)
    with _admitted_policy(command, policy, root) as gate:
        yield gate


@contextmanager
def readonly_wait_window() -> Iterator[None]:
    """Narrow the live shared hold to a read-only remote-wait phase (#940).

    Usable only inside an audited shared-admission command whose read-only
    wait phase provably performs no local mutation (the retained
    orchestrate run -> dispatch run-one -> wait chain). The resource hold is
    released for the window so an exclusive admission - specifically the
    owner-lock-tolerating ``apply --hot-plugin-docs`` exception - may enter;
    ordinary upgrades keep their own global-quiescence gates and still
    refuse. Reacquisition serializes behind an active exclusive window or a
    waiting writer and repeats the shared re-entry checks (mutation policy,
    OPEN lifecycle, trusted bootstrap identity); any violated check refuses.
    """
    active = _ACTIVE_SHARED.get()
    if active is None:
        # An unadmitted in-process caller holds no admission lock, so there
        # is nothing to narrow and no lock state can change.
        yield
        return
    gate = active.gate
    command = active.command
    if gate.exclusive or command not in _READONLY_WAIT_COMMANDS:
        raise AdmissionError("readonly wait window is not valid for this admission")
    holds = gate.holds
    if holds is None or holds.deferred:
        raise AdmissionError("readonly wait window requires its own admitted shared hold")
    fcntl.flock(holds.resource, fcntl.LOCK_UN)
    holds.deferred = True
    try:
        yield
    finally:
        holds.deferred = False
        fcntl.flock(holds.turnstile, fcntl.LOCK_EX)
        try:
            fcntl.flock(holds.resource, fcntl.LOCK_SH)
        finally:
            fcntl.flock(holds.turnstile, fcntl.LOCK_UN)
        if mutation_policy(command) != "shared-admission":
            raise AdmissionError("mutation policy changed during readonly wait")
        if lifecycle_state(gate.root) is not LifecycleState.OPEN:
            raise AdmissionError("shared re-entry requires OPEN; recovery is required")
        if not trusted_bootstrap_identity_matches(gate.root):
            raise AdmissionError("bootstrap identity does not match current admission contract")


@contextmanager
def _admitted_policy(
    command: str,
    policy: str,
    root: Path | None,
    *,
    allow_identity_transition: bool = False,
) -> Iterator[AdmissionGate]:
    if policy == "read-only":
        raise AdmissionError("read-only command has no mutation authority")
    with admission_gate(
        root,
        exclusive=policy != "shared-admission",
        bypass_waiting_writer=command
        in {
            "omx_task_signal:checkpoint",
            "omx_task_signal:received",
            "omx_task_signal:started",
            "omx_task_signal:heartbeat",
            "omx_task_signal:finish",
        },
    ) as gate:
        if mutation_policy(command) != policy:
            raise AdmissionError("mutation policy changed during admission")
        state = lifecycle_state(gate.root)
        if allow_identity_transition:
            if (
                command != "omx_install_sync:apply"
                or policy != "exclusive-admission"
                or state
                not in {
                    LifecycleState.OPEN,
                    LifecycleState.RECOVERY_REQUIRED,
                }
            ):
                raise AdmissionError("runtime identity transition admission invalid")
        elif policy == "recovery-only":
            if state is not LifecycleState.RECOVERY_REQUIRED:
                raise AdmissionError("recovery-only command requires RECOVERY_REQUIRED")
        elif command == "omx_install_sync:bootstrap-companion-skills":
            if state not in {LifecycleState.UNINITIALIZED, LifecycleState.OPEN}:
                raise AdmissionError(f"bootstrap command requires UNINITIALIZED or OPEN, got {state}")
        elif command == "omx_install_sync:companion-release-activate":
            if state is not LifecycleState.OPEN:
                raise AdmissionError(f"companion-release-activate requires OPEN, got {state}")
        elif state is not LifecycleState.OPEN:
            raise AdmissionError(f"ordinary command requires OPEN, got {state}")
        if command not in {
            "omx_install_sync:bootstrap-companion-skills",
            "omx_install_sync:companion-release-activate",
        } and not allow_identity_transition and not trusted_bootstrap_identity_matches(gate.root):
            raise AdmissionError("bootstrap identity does not match current admission contract")
        if policy == "shared-admission":
            token = _ACTIVE_SHARED.set(_ActiveSharedAdmission(gate, command))
            try:
                yield gate
            finally:
                _ACTIVE_SHARED.reset(token)
        else:
            yield gate
