"""Single mutation boundary for the OpenCode visible-worker registry.

Invariants:
- registration may create a missing row or refresh the same binding;
- changing an existing binding requires an explicit sanctioned path;
- validation and mutation use the freshly read payload while holding one lock;
- persistence is atomic, and stale planned writes fail closed by hash.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


RegistryMutator = Callable[[dict[str, Any]], dict[str, Any]]
BackupFactory = Callable[[Path], Path]


class VisibleRegistryError(RuntimeError):
    """Expected visible-registry mutation failure."""


class VisibleRegistryConflict(VisibleRegistryError):
    """The registry changed after a caller planned its mutation."""


class VisibleRegistryLeaseConflict(VisibleRegistryError):
    """A binding lease mutation does not match the current owner or state."""


def normalize_visible_stop_fence(
    record: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    """Return the exact task-owned binding identity allowed for pane cleanup."""
    if not isinstance(record, dict):
        raise VisibleRegistryError("expected stop fence must be a JSON object")
    if record.get("worker_id") != worker_id:
        raise VisibleRegistryError("expected stop fence worker_id mismatch")
    if record.get("launch_owned") is not True:
        raise VisibleRegistryError(
            "expected stop fence requires launch_owned=true"
        )
    if record.get("user_started") is not False:
        raise VisibleRegistryError(
            "expected stop fence requires user_started=false"
        )
    text_fields = (
        "worker_id",
        "instance_id",
        "binding_lease_id",
        "pane_id",
        "tmux_session",
        "tmux_window",
    )
    normalized: dict[str, Any] = {}
    for field in text_fields:
        value = record.get(field)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise VisibleRegistryError(
                f"expected stop fence {field} must be a nonempty string"
            )
        normalized[field] = value
    if not re.fullmatch(r"%\d+", normalized["pane_id"]):
        raise VisibleRegistryError("expected stop fence pane_id is invalid")
    for field in (
        "binding_generation",
        "fencing_generation",
        "pane_pid",
        "port",
    ):
        value = record.get(field)
        if type(value) is not int:
            raise VisibleRegistryError(
                f"expected stop fence {field} is invalid"
            )
        normalized[field] = value
        if normalized[field] <= 0:
            raise VisibleRegistryError(
                f"expected stop fence {field} is invalid"
            )
    if normalized["port"] > 65535:
        raise VisibleRegistryError("expected stop fence port is invalid")
    normalized["launch_owned"] = True
    normalized["user_started"] = False
    return normalized


def normalize_visible_orphan_fence(
    record: dict[str, Any],
    worker_id: str,
) -> dict[str, Any]:
    """Return the exact process identity allowed for orphan cleanup."""
    normalized = normalize_visible_stop_fence(record, worker_id)
    for field in (
        "backend",
        "host",
        "host_boot_id",
        "binding_confidence",
    ):
        value = record.get(field)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise VisibleRegistryError(
                f"expected orphan fence {field} must be a nonempty string"
            )
        normalized[field] = value
    if normalized["backend"] != "opencode_tui":
        raise VisibleRegistryError(
            "expected orphan fence backend must be opencode_tui"
        )
    if normalized["host"] != "127.0.0.1":
        raise VisibleRegistryError(
            "expected orphan fence host must be 127.0.0.1"
        )
    if normalized["binding_confidence"] != "strong":
        raise VisibleRegistryError(
            "expected orphan fence binding_confidence must be strong"
        )
    for field in (
        "pane_start_time_ticks",
        "opencode_pid",
        "opencode_start_time_ticks",
    ):
        value = record.get(field)
        if type(value) is not int or value <= 0:
            raise VisibleRegistryError(
                f"expected orphan fence {field} is invalid"
            )
        normalized[field] = value
    listener_pids = record.get("listener_pids")
    if (
        not isinstance(listener_pids, list)
        or not listener_pids
        or any(type(pid) is not int or pid <= 0 for pid in listener_pids)
        or len(set(listener_pids)) != len(listener_pids)
        or normalized["opencode_pid"] not in listener_pids
    ):
        raise VisibleRegistryError(
            "expected orphan fence listener_pids is invalid"
        )
    normalized["listener_pids"] = list(listener_pids)
    return normalized


def visible_stop_fence_matches(
    expected: dict[str, Any],
    current: dict[str, Any] | None,
    worker_id: str,
) -> bool:
    if current is None:
        return False
    try:
        return (
            normalize_visible_stop_fence(expected, worker_id)
            == normalize_visible_stop_fence(current, worker_id)
        )
    except VisibleRegistryError:
        return False


IDENTITY_SCHEMA_VERSION = 1
LEASE_STATES = {
    "unmanaged",
    "active",
    "expired",
    "orphaned",
    "stale",
    "released",
}


def advance_binding_generations(record: dict[str, Any]) -> dict[str, Any]:
    """Advance the binding version and its side-effect fencing generation."""
    binding_generation = _positive_int(record.get("binding_generation"))
    fencing_generation = _positive_int(record.get("fencing_generation"))
    record["binding_generation"] = binding_generation + 1 if binding_generation else 1
    record["fencing_generation"] = max(
        record["binding_generation"],
        fencing_generation + 1 if fencing_generation else 1,
    )
    return record


def derive_process_instance(
    record: dict[str, Any],
    binding: dict[str, Any] | None = None,
    *,
    observed_at: str = "",
) -> dict[str, Any]:
    """Build one persisted identity for a concrete OpenCode process lifetime."""
    binding = binding or {}
    observed_at = observed_at or _utc_now()
    opencode_pid = _first_positive_int(
        binding.get("opencode_pid"),
        record.get("opencode_pid"),
        *_list_values(binding.get("listener_pids")),
        *_list_values(record.get("listener_pids")),
        *_list_values(binding.get("opencode_pids")),
        *_list_values(record.get("opencode_pids")),
    )
    evidence = {
        "host": str(binding.get("host") or record.get("host") or "127.0.0.1"),
        "host_boot_id": str(
            binding.get("host_boot_id") or record.get("host_boot_id") or ""
        ),
        "tmux_server_pid": _positive_int(
            binding.get("tmux_server_pid", record.get("tmux_server_pid"))
        ),
        "pane_id": str(binding.get("pane_id") or record.get("pane_id") or ""),
        "pane_pid": _positive_int(binding.get("pane_pid", record.get("pane_pid"))),
        "pane_start_time_ticks": _positive_int(
            binding.get(
                "pane_start_time_ticks",
                record.get("pane_start_time_ticks"),
            )
        ),
        "opencode_pid": opencode_pid,
        "opencode_start_time_ticks": _positive_int(
            binding.get(
                "opencode_start_time_ticks",
                record.get("opencode_start_time_ticks"),
            )
        ),
        "port": _positive_int(binding.get("port", record.get("port"))),
    }
    process_lifecycle = bool(
        evidence["host_boot_id"]
        and evidence["pane_pid"]
        and evidence["pane_start_time_ticks"]
        and evidence["opencode_pid"]
        and evidence["opencode_start_time_ticks"]
        and evidence["port"]
    )
    identity_material = dict(evidence)
    if not process_lifecycle:
        identity_material["legacy_worker_id"] = str(
            record.get("worker_id") or binding.get("worker_id") or ""
        )
    explicit = str(binding.get("instance_id") or record.get("instance_id") or "").strip()
    explicit_strength = str(
        binding.get("identity_strength") or record.get("identity_strength") or ""
    )
    digest = hashlib.sha256(
        json.dumps(identity_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    derived_instance_id = f"vw-inst-{digest}"
    preserve_explicit = bool(
        explicit
        and (
            not process_lifecycle
            or (explicit_strength == "process_lifecycle" and explicit == derived_instance_id)
        )
    )
    return {
        "instance_id": explicit if preserve_explicit else derived_instance_id,
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "identity_strength": "process_lifecycle" if process_lifecycle else "legacy_derived",
        **evidence,
        "created_at": str(
            binding.get("instance_created_at")
            or record.get("instance_created_at")
            or observed_at
        ),
        "last_seen_at": observed_at,
    }


def normalize_visible_registry(
    registry: dict[str, Any],
    *,
    controller_id: str = "",
    observed_at: str = "",
) -> dict[str, Any]:
    """Add normalized instances/bindings while preserving legacy worker rows."""
    normalized = copy.deepcopy(registry)
    observed_at = observed_at or _utc_now()
    normalized["identity_schema_version"] = IDENTITY_SCHEMA_VERSION
    normalized["instances"] = [
        dict(item)
        for item in normalized.get("instances", [])
        if isinstance(item, dict) and str(item.get("instance_id") or "")
    ]
    normalized["bindings"] = [
        dict(item)
        for item in normalized.get("bindings", [])
        if isinstance(item, dict)
        and str(item.get("scope_id") or "")
        and str(item.get("alias") or "")
    ]
    workers = [
        dict(item)
        for item in normalized.get("workers", [])
        if isinstance(item, dict) and str(item.get("worker_id") or "")
    ]
    projected_workers: list[dict[str, Any]] = []

    for worker in workers:
        worker_id = str(worker.get("worker_id") or "")
        port = _positive_int(worker.get("port"))
        _enforce_alias_port(worker_id, port)
        instance = derive_process_instance(worker, observed_at=observed_at)
        instance = _upsert_instance(normalized, instance)
        worker.update(
            {
                "instance_id": instance["instance_id"],
                "identity_strength": instance["identity_strength"],
                "host_boot_id": instance["host_boot_id"],
                "tmux_server_pid": instance["tmux_server_pid"],
                "pane_start_time_ticks": instance["pane_start_time_ticks"],
                "opencode_pid": instance["opencode_pid"],
                "opencode_start_time_ticks": instance["opencode_start_time_ticks"],
            }
        )
        projected_workers.append(worker)
        if port:
            _upsert_binding(
                normalized,
                _binding_from_worker(
                    worker,
                    instance_id=instance["instance_id"],
                    scope_id="global",
                    alias=f"vw-port-{port}",
                    observed_at=observed_at,
                ),
            )
        legacy_scope = "global" if worker_id.startswith("vw-port-") else "legacy-global"
        _upsert_binding(
            normalized,
            _binding_from_worker(
                worker,
                instance_id=instance["instance_id"],
                scope_id=legacy_scope,
                alias=worker_id,
                observed_at=observed_at,
            ),
        )
        if controller_id and port:
            _upsert_binding(
                normalized,
                _binding_from_worker(
                    worker,
                    instance_id=instance["instance_id"],
                    scope_id=f"controller:{controller_id}",
                    alias=f"current/{port}",
                    observed_at=observed_at,
                ),
            )

    normalized["instances"].sort(key=lambda item: str(item.get("instance_id") or ""))
    normalized["bindings"].sort(
        key=lambda item: (
            str(item.get("scope_id") or ""),
            str(item.get("alias") or ""),
        )
    )
    normalized["workers"] = sorted(
        projected_workers,
        key=lambda item: str(item.get("worker_id") or ""),
    )
    return normalized


def replace_visible_worker_binding(
    registry: dict[str, Any],
    *,
    worker_id: str,
    replacement_record: dict[str, Any],
    expected_instance_id: str,
    controller_id: str = "",
    observed_at: str = "",
) -> dict[str, Any]:
    """Retarget one compatibility worker and its aliases after sanctioned rebind."""
    observed_at = observed_at or _utc_now()
    current = next(
        (
            dict(item)
            for item in registry.get("workers", [])
            if isinstance(item, dict)
            and str(item.get("worker_id") or "") == worker_id
        ),
        None,
    )
    if current is None:
        raise VisibleRegistryError(f"visible worker not found: {worker_id}")
    current_instance = derive_process_instance(current, observed_at=observed_at)
    current_instance_id = str(current_instance.get("instance_id") or "")
    if current_instance_id != expected_instance_id:
        raise VisibleRegistryConflict(
            "instance_id changed before sanctioned visible-worker rebind"
        )

    normalization_seed = copy.deepcopy(registry)
    normalization_seed["workers"] = [
        dict(item)
        for item in registry.get("workers", [])
        if isinstance(item, dict)
        and str(item.get("worker_id") or "") != worker_id
    ]
    normalized = normalize_visible_registry(
        normalization_seed,
        controller_id=controller_id,
        observed_at=observed_at,
    )
    _upsert_instance(normalized, current_instance)
    for binding in normalized.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if str(binding.get("instance_id") or "") != current_instance_id:
            continue
        if str(binding.get("lease_status") or "unmanaged") not in {
            "unmanaged",
            "released",
        }:
            raise VisibleRegistryLeaseConflict(
                "active lease or any non-released lease blocks rebind; "
                "use explicit sanctioned lease takeover"
            )

    replacement = {**current, **replacement_record, "worker_id": worker_id}
    _enforce_alias_port(worker_id, _positive_int(replacement.get("port")))
    identity_input = dict(replacement)
    identity_input.pop("instance_id", None)
    identity_input.pop("identity_strength", None)
    instance = derive_process_instance(identity_input, observed_at=observed_at)
    instance = _upsert_instance(normalized, instance)
    replacement.update(
        {
            "instance_id": instance["instance_id"],
            "identity_strength": instance["identity_strength"],
            "host_boot_id": instance["host_boot_id"],
            "tmux_server_pid": instance["tmux_server_pid"],
            "pane_start_time_ticks": instance["pane_start_time_ticks"],
            "opencode_pid": instance["opencode_pid"],
            "opencode_start_time_ticks": instance["opencode_start_time_ticks"],
        }
    )
    replacement_generation = _positive_int(replacement.get("binding_generation")) or 1
    replacement_fence = max(
        replacement_generation,
        _positive_int(replacement.get("fencing_generation")) or replacement_generation,
    )
    replacement_port = _positive_int(replacement.get("port"))
    normalized["bindings"] = [
        binding
        for binding in normalized.get("bindings", [])
        if not (
            isinstance(binding, dict)
            and str(binding.get("instance_id") or "") == current_instance_id
            and _derived_binding_port(binding)
            and _derived_binding_port(binding) != replacement_port
        )
    ]
    for binding in normalized.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if str(binding.get("instance_id") or "") != current_instance_id:
            continue
        binding.update(
            {
                "instance_id": instance["instance_id"],
                "host": str(replacement.get("host") or "127.0.0.1"),
                "port": replacement_port,
                "binding_generation": replacement_generation,
                "fencing_generation": replacement_fence,
                "updated_at": observed_at,
            }
        )
    normalized["workers"].append(replacement)
    return normalize_visible_registry(
        normalized,
        controller_id=controller_id,
        observed_at=observed_at,
    )


def claim_visible_binding(
    registry: dict[str, Any],
    *,
    scope_id: str,
    alias: str,
    owner_controller_id: str,
    lease_id: str,
    now: str,
    expires_at: str,
) -> dict[str, Any]:
    binding = _require_binding(registry, scope_id, alias)
    _require_nonempty(owner_controller_id, "owner_controller_id")
    _require_nonempty(lease_id, "lease_id")
    _require_future_expiry(now, expires_at)
    status = str(binding.get("lease_status") or "unmanaged")
    current_owner = str(binding.get("lease_owner") or "")
    current_lease = str(binding.get("lease_id") or "")
    if status == "active" and current_owner == owner_controller_id and current_lease == lease_id:
        binding["lease_renewed_at"] = now
        binding["lease_expires_at"] = expires_at
        _append_binding_audit(registry, binding, "renew", now, "idempotent_claim")
        return binding
    if status not in {"unmanaged", "released"}:
        raise VisibleRegistryLeaseConflict(
            f"binding lease is {status}; explicit sanctioned takeover is required"
        )
    binding.update(
        {
            "lease_status": "active",
            "lease_owner": owner_controller_id,
            "lease_id": lease_id,
            "lease_claimed_at": now,
            "lease_renewed_at": now,
            "lease_expires_at": expires_at,
            "lease_release_reason": "",
        }
    )
    _append_binding_audit(registry, binding, "claim", now, "lease_claimed")
    return binding


def renew_visible_binding(
    registry: dict[str, Any],
    *,
    scope_id: str,
    alias: str,
    owner_controller_id: str,
    lease_id: str,
    now: str,
    expires_at: str,
) -> dict[str, Any]:
    binding = _require_binding(registry, scope_id, alias)
    _require_lease_owner(binding, owner_controller_id, lease_id)
    if str(binding.get("lease_status") or "") != "active":
        raise VisibleRegistryLeaseConflict(
            f"cannot renew lease in state {binding.get('lease_status') or 'unmanaged'}"
        )
    _require_future_expiry(now, expires_at)
    binding["lease_renewed_at"] = now
    binding["lease_expires_at"] = expires_at
    _append_binding_audit(registry, binding, "renew", now, "lease_renewed")
    return binding


def release_visible_binding(
    registry: dict[str, Any],
    *,
    scope_id: str,
    alias: str,
    owner_controller_id: str,
    lease_id: str,
    now: str,
    reason: str,
) -> dict[str, Any]:
    binding = _require_binding(registry, scope_id, alias)
    _require_lease_owner(binding, owner_controller_id, lease_id)
    _require_nonempty(reason, "reason")
    binding.update(
        {
            "lease_status": "released",
            "lease_released_at": now,
            "lease_release_reason": reason,
        }
    )
    _append_binding_audit(registry, binding, "release", now, reason)
    return binding


def reconcile_visible_binding_lease(
    registry: dict[str, Any],
    *,
    scope_id: str,
    alias: str,
    now: str,
    worker_health: str,
) -> dict[str, Any]:
    binding = _require_binding(registry, scope_id, alias)
    if worker_health not in {"healthy", "unknown", "stale"}:
        raise VisibleRegistryError(f"invalid worker_health: {worker_health}")
    status = str(binding.get("lease_status") or "unmanaged")
    expires_at = str(binding.get("lease_expires_at") or "")
    if status == "active" and expires_at and _parse_utc(expires_at) <= _parse_utc(now):
        binding["lease_status"] = "stale" if worker_health == "stale" else "orphaned"
        binding["lease_expired_at"] = now
        binding["lease_worker_health"] = worker_health
        _append_binding_audit(
            registry,
            binding,
            "expire",
            now,
            "worker_stale" if worker_health == "stale" else "worker_not_proven_stale",
        )
    return binding


def sanctioned_takeover_visible_binding(
    registry: dict[str, Any],
    *,
    scope_id: str,
    alias: str,
    expected_instance_id: str,
    expected_binding_generation: int,
    expected_fencing_generation: int,
    new_instance_id: str,
    owner_controller_id: str,
    lease_id: str,
    now: str,
    expires_at: str,
    reason: str,
    worker_health: str,
) -> dict[str, Any]:
    binding = _require_binding(registry, scope_id, alias)
    if worker_health != "stale" or str(binding.get("lease_status") or "") != "stale":
        raise VisibleRegistryLeaseConflict(
            "sanctioned takeover requires a confirmed stale worker and stale lease"
        )
    _require_nonempty(reason, "reason")
    _require_nonempty(owner_controller_id, "owner_controller_id")
    _require_nonempty(lease_id, "lease_id")
    _require_future_expiry(now, expires_at)
    if str(binding.get("instance_id") or "") != expected_instance_id:
        raise VisibleRegistryConflict("instance_id changed before sanctioned takeover")
    if _positive_int(binding.get("binding_generation")) != int(expected_binding_generation):
        raise VisibleRegistryConflict("binding_generation changed before sanctioned takeover")
    if _positive_int(binding.get("fencing_generation")) != int(expected_fencing_generation):
        raise VisibleRegistryConflict("fencing_generation changed before sanctioned takeover")
    if not any(
        str(item.get("instance_id") or "") == new_instance_id
        for item in registry.get("instances", [])
        if isinstance(item, dict)
    ):
        raise VisibleRegistryError(f"new instance is not registered: {new_instance_id}")
    binding.update(
        {
            "instance_id": new_instance_id,
            "binding_generation": int(expected_binding_generation) + 1,
            "fencing_generation": max(
                int(expected_binding_generation) + 1,
                int(expected_fencing_generation) + 1,
            ),
            "lease_status": "active",
            "lease_owner": owner_controller_id,
            "lease_id": lease_id,
            "lease_claimed_at": now,
            "lease_renewed_at": now,
            "lease_expires_at": expires_at,
            "takeover_reason": reason,
            "takeover_at": now,
        }
    )
    _append_binding_audit(registry, binding, "sanctioned_takeover", now, reason)
    return binding


def visible_registry_lock_path(registry_path: Path) -> Path:
    """Reuse the established rebind lock so old/new writers share one fence."""
    return registry_path.parent / "rebind" / ".registry-rebind.lock"


def mutate_visible_registry(
    registry_path: Path,
    mutator: RegistryMutator,
    *,
    expected_pre_hash: str = "",
    default_payload: dict[str, Any] | None = None,
    backup_factory: BackupFactory | None = None,
) -> dict[str, Any]:
    """Lock, read, validate/mutate, atomically replace, then unlock."""
    registry_path = registry_path.resolve()
    with _exclusive_registry_lock(registry_path):
        before = _read_registry(registry_path, default_payload)
        pre_hash = _sha256_file(registry_path)
        if expected_pre_hash and pre_hash != expected_pre_hash:
            raise VisibleRegistryConflict(
                "registry changed since planning: "
                f"expected {expected_pre_hash}, got {pre_hash or '<missing>'}"
            )

        after = mutator(copy.deepcopy(before))
        if not isinstance(after, dict):
            raise VisibleRegistryError("registry mutator must return a JSON object")

        changed = after != before
        backup_path = ""
        if changed:
            if backup_factory is not None and registry_path.exists():
                backup_path = str(backup_factory(registry_path))
            _atomic_write_json(registry_path, after)

        return {
            "before": before,
            "after": after,
            "changed": changed,
            "pre_hash": pre_hash,
            "post_hash": _sha256_file(registry_path),
            "backup_path": backup_path,
            "lock_path": str(visible_registry_lock_path(registry_path)),
        }


@contextmanager
def _exclusive_registry_lock(registry_path: Path) -> Iterator[None]:
    lock_path = visible_registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_registry(path: Path, default_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default_payload or {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VisibleRegistryError(f"cannot read visible registry {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise VisibleRegistryError(f"visible registry must contain a JSON object: {path}")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VisibleRegistryError(f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _list_values(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _first_positive_int(*values: Any) -> int:
    for value in values:
        parsed = _positive_int(value)
        if parsed:
            return parsed
    return 0


def _expected_port(alias: str) -> int:
    match = re.match(r"^vw-(?:current|port)-?([0-9]+)(?:[@:].*)?$", alias)
    return int(match.group(1)) if match else 0


def _derived_binding_port(binding: dict[str, Any]) -> int:
    scope_id = str(binding.get("scope_id") or "")
    alias = str(binding.get("alias") or "")
    if scope_id == "global":
        match = re.fullmatch(r"vw-port-([0-9]+)", alias)
    elif scope_id.startswith("controller:"):
        match = re.fullmatch(r"current/([0-9]+)", alias)
    else:
        match = None
    return int(match.group(1)) if match else 0


def _enforce_alias_port(alias: str, port: int) -> None:
    expected = _expected_port(alias)
    if expected and expected != port:
        raise VisibleRegistryError(
            f"alias port mismatch: {alias!r} implies port {expected}, got {port}"
        )


def _upsert_instance(
    registry: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    instance_id = str(incoming.get("instance_id") or "")
    instances = registry.setdefault("instances", [])
    for index, current in enumerate(instances):
        if not isinstance(current, dict):
            continue
        if str(current.get("instance_id") or "") != instance_id:
            continue
        conflicts = [
            field
            for field in (
                "host_boot_id",
                "pane_pid",
                "pane_start_time_ticks",
                "opencode_pid",
                "opencode_start_time_ticks",
                "port",
            )
            if current.get(field)
            and incoming.get(field)
            and str(current.get(field)) != str(incoming.get(field))
        ]
        if conflicts:
            raise VisibleRegistryConflict(
                f"instance_id {instance_id} has conflicting lifecycle fields: "
                + ", ".join(conflicts)
            )
        merged = {**current, **incoming}
        merged["created_at"] = str(current.get("created_at") or incoming.get("created_at") or "")
        instances[index] = merged
        return merged
    instances.append(incoming)
    return incoming


def _binding_id(scope_id: str, alias: str) -> str:
    digest = hashlib.sha256(f"{scope_id}\0{alias}".encode("utf-8")).hexdigest()[:16]
    return f"vwb-{digest}"


def _binding_from_worker(
    worker: dict[str, Any],
    *,
    instance_id: str,
    scope_id: str,
    alias: str,
    observed_at: str,
) -> dict[str, Any]:
    port = _positive_int(worker.get("port"))
    _enforce_alias_port(alias, port)
    binding_generation = _positive_int(worker.get("binding_generation")) or 1
    fencing_generation = max(
        binding_generation,
        _positive_int(worker.get("fencing_generation")) or binding_generation,
    )
    return {
        "binding_id": _binding_id(scope_id, alias),
        "scope_id": scope_id,
        "alias": alias,
        "instance_id": instance_id,
        "host": str(worker.get("host") or "127.0.0.1"),
        "port": port,
        "binding_generation": binding_generation,
        "fencing_generation": fencing_generation,
        "lease_status": str(worker.get("lease_status") or "unmanaged"),
        "lease_owner": str(worker.get("lease_owner") or ""),
        "lease_id": str(worker.get("lease_id") or ""),
        "created_at": str(worker.get("binding_created_at") or observed_at),
        "updated_at": observed_at,
    }


def _upsert_binding(
    registry: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    scope_id = str(incoming.get("scope_id") or "")
    alias = str(incoming.get("alias") or "")
    bindings = registry.setdefault("bindings", [])
    for index, current in enumerate(bindings):
        if not isinstance(current, dict):
            continue
        if (
            str(current.get("scope_id") or "") != scope_id
            or str(current.get("alias") or "") != alias
        ):
            continue
        if str(current.get("instance_id") or "") != str(incoming.get("instance_id") or ""):
            same_physical_binding = all(
                str(current.get(field) or "") == str(incoming.get(field) or "")
                for field in ("host", "port")
            )
            if not same_physical_binding:
                raise VisibleRegistryConflict(
                    f"binding {scope_id}:{alias} already points to a different instance"
                )
        merged = {**incoming, **current}
        merged["instance_id"] = incoming.get("instance_id")
        merged["host"] = incoming.get("host")
        merged["port"] = incoming.get("port")
        merged["updated_at"] = incoming.get("updated_at")
        merged["binding_generation"] = max(
            _positive_int(current.get("binding_generation")),
            _positive_int(incoming.get("binding_generation")),
        )
        merged["fencing_generation"] = max(
            merged["binding_generation"],
            _positive_int(current.get("fencing_generation")),
            _positive_int(incoming.get("fencing_generation")),
        )
        bindings[index] = merged
        return merged
    bindings.append(incoming)
    return incoming


def _require_binding(
    registry: dict[str, Any],
    scope_id: str,
    alias: str,
) -> dict[str, Any]:
    for binding in registry.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if (
            str(binding.get("scope_id") or "") == scope_id
            and str(binding.get("alias") or "") == alias
        ):
            return binding
    raise VisibleRegistryError(f"visible binding not found: {scope_id}:{alias}")


def _require_nonempty(value: str, field: str) -> None:
    if not str(value or "").strip():
        raise VisibleRegistryError(f"{field} is required")


def _require_future_expiry(now: str, expires_at: str) -> None:
    if _parse_utc(expires_at) <= _parse_utc(now):
        raise VisibleRegistryError("lease expiry must be after the mutation time")


def _require_lease_owner(
    binding: dict[str, Any],
    owner_controller_id: str,
    lease_id: str,
) -> None:
    if (
        str(binding.get("lease_owner") or "") != owner_controller_id
        or str(binding.get("lease_id") or "") != lease_id
    ):
        raise VisibleRegistryLeaseConflict("lease owner mismatch")


def _append_binding_audit(
    registry: dict[str, Any],
    binding: dict[str, Any],
    action: str,
    at: str,
    reason: str,
) -> None:
    audit = registry.setdefault("binding_audit", [])
    audit.append(
        {
            "audit_kind": "visible_binding_lease",
            "action": action,
            "scope_id": str(binding.get("scope_id") or ""),
            "alias": str(binding.get("alias") or ""),
            "instance_id": str(binding.get("instance_id") or ""),
            "binding_generation": _positive_int(binding.get("binding_generation")),
            "fencing_generation": _positive_int(binding.get("fencing_generation")),
            "lease_id": str(binding.get("lease_id") or ""),
            "lease_owner": str(binding.get("lease_owner") or ""),
            "lease_status": str(binding.get("lease_status") or "unmanaged"),
            "reason": reason,
            "at": at,
        }
    )


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0
