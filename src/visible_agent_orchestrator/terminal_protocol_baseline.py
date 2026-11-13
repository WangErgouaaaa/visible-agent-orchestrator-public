from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from visible_agent_orchestrator.terminal_protocol_constants import (
    CREATION_RECORD_SUFFIX,
    FENCE_RECORD_KIND,
    IDENTITY_FIELDS,
    LEGACY_ACTIVATION_BATCH_FILENAME,
    LEGACY_BASELINE_KIND,
    LEGACY_BASELINE_SCHEMA_VERSION,
    LEGACY_BASELINE_FILENAME,
    PROTOCOL_ALIASES,
    TERMINAL_OUTPUT_CLAIM_KIND,
    OPERATOR_RECOVERY_CLAIM_KIND,
)


_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "run_id": ("run_id", "run"),
    "task_id": ("task_id", "task", "id"),
    "worker_id": ("worker_id", "worker", "owner"),
    "attempt_id": ("attempt_id",),
    "contract_hash": ("contract_hash",),
    "session_id": ("session_id",),
    "instance_id": ("instance_id",),
    "binding_generation": ("binding_generation",),
    "fencing_generation": ("fencing_generation",),
}

_LEGACY_EVIDENCE_NAMES: Final = (
    "task.json",
    "dispatch.json",
    "acceptance_fence.json",
    "terminal_output_claim.json",
    "report.md",
    "creation_record",
)
_CORE_RECORD_NAMES: Final = _LEGACY_EVIDENCE_NAMES[:4]
_EXCLUSION_REASONS: Final = frozenset(
    (
        "empty_report",
        "invalid_legacy_identity",
        "invalid_legacy_record",
        "malformed_acceptance_fence_json",
        "malformed_creation_record_json",
        "malformed_dispatch_json",
        "malformed_task_json",
        "malformed_terminal_output_claim_json",
        "missing_report",
    )
)


def read_regular_file_bytes(path: Path, *, max_bytes: int | None = None) -> tuple[bool, bytes, bool]:
    """Return absent, safely read regular bytes, or an unsafe/unreadable state."""
    current = -1
    descriptor = -1
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        parts = path.parts
        if path.is_absolute():
            current = os.open(os.sep, directory_flags)
            parts = parts[1:]
        else:
            current = os.open(".", directory_flags)
        if not parts:
            raise OSError("regular file path must name a file")
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = child
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
    except FileNotFoundError:
        if current >= 0:
            os.close(current)
        return False, b"", False
    except OSError:
        if current >= 0:
            os.close(current)
        return True, b"", True
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return True, b"", True
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(
            descriptor,
            1024 * 1024 if max_bytes is None else max_bytes - total + 1,
        ):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                return True, b"", True
            chunks.append(chunk)
        observed = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if (
            not stat.S_ISREG(observed.st_mode)
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
        ):
            return True, b"", True
        return True, b"".join(chunks), False
    except OSError:
        return True, b"", True
    finally:
        os.close(descriptor)
        if current >= 0:
            os.close(current)


def legacy_activation_batch_id(
    activation_generation: str,
    activation_cutoff: str,
    run_ids: list[str],
) -> str:
    payload = json.dumps(
        {
            "activation_cutoff": activation_cutoff,
            "activation_generation": activation_generation,
            "run_ids": run_ids,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json_record(path: Path) -> tuple[bool, dict[str, Any], bool]:
    present, raw, unsafe = read_regular_file_bytes(path)
    if not present:
        return False, {}, False
    if unsafe:
        return True, {}, True
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return True, {}, True
    return True, payload, not isinstance(payload, dict)


def _run_root(task_dir: Path) -> Path:
    return task_dir.parent.parent


def legacy_baseline_path(task_dir: Path) -> Path:
    return _run_root(task_dir) / LEGACY_BASELINE_FILENAME


def creation_record_path(task_dir: Path) -> Path:
    return _run_root(task_dir) / "task-contracts" / f"{task_dir.name}{CREATION_RECORD_SUFFIX}"


def _report_hash(task_dir: Path) -> str:
    present, report, unsafe = read_regular_file_bytes(task_dir / "report.md")
    if not present or unsafe:
        return ""
    if not report.strip():
        return ""
    return hashlib.sha256(report).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _activation_batch_errors(
    task_dir: Path,
    baseline: dict[str, Any],
    baseline_bytes: bytes,
) -> list[str]:
    batch_id = baseline.get("activation_batch_id")
    scope = baseline.get("activation_scope")
    if scope is None and batch_id is None:
        return []
    if scope != "all_runs_batch":
        return ["legacy_baseline_activation_scope"]
    if not _is_sha256(batch_id):
        return ["legacy_activation_batch_id"]
    batch_path = _run_root(task_dir).parent / LEGACY_ACTIVATION_BATCH_FILENAME
    present, batch, malformed = _read_json_record(batch_path)
    if not present:
        return ["legacy_activation_batch_missing"]
    if malformed:
        return ["legacy_activation_batch"]
    errors: list[str] = []
    if type(batch.get("schema_version")) is not int or batch.get("schema_version") != 1:
        errors.append("legacy_activation_batch_schema_version")
    if batch.get("batch_kind") != "legacy_terminal_activation_batch":
        errors.append("legacy_activation_batch_kind")
    if batch.get("phase") != "SEALED" or batch.get("sealed") is not True:
        errors.append("legacy_activation_batch_sealed")
    for field in ("activation_generation", "activation_cutoff"):
        if not isinstance(batch.get(field), str) or not batch[field].strip():
            errors.append(f"legacy_activation_batch_{field}")
        elif baseline.get(field) != batch[field]:
            errors.append(f"legacy_activation_batch_{field}_conflict")
    run_ids = batch.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or not run_ids
        or any(not isinstance(run, str) or not run for run in run_ids)
        or run_ids != sorted(set(run_ids))
    ):
        errors.append("legacy_activation_batch_run_inventory")
        run_ids = []
    targets = batch.get("target_sha256")
    if not isinstance(targets, dict) or set(targets) != set(run_ids):
        errors.append("legacy_activation_batch_target_inventory")
        targets = {}
    target_paths = batch.get("target_paths")
    if not isinstance(target_paths, dict) or set(target_paths) != set(run_ids):
        errors.append("legacy_activation_batch_target_path_inventory")
        target_paths = {}
    for run in run_ids:
        if not _is_sha256(targets.get(run)):
            errors.append("legacy_activation_batch_target_hash")
        if target_paths.get(run) != f"{run}/{LEGACY_BASELINE_FILENAME}":
            errors.append("legacy_activation_batch_target_path")
    if batch.get("activation_batch_id") != batch_id:
        errors.append("legacy_activation_batch_id_conflict")
    if (
        isinstance(batch.get("activation_generation"), str)
        and isinstance(batch.get("activation_cutoff"), str)
        and isinstance(run_ids, list)
        and batch.get("activation_batch_id")
        != legacy_activation_batch_id(
            batch["activation_generation"],
            batch["activation_cutoff"],
            run_ids,
        )
    ):
        errors.append("legacy_activation_batch_id")
    run_id = _run_root(task_dir).name
    if run_id not in run_ids:
        errors.append("legacy_activation_batch_membership")
    else:
        actual_hash = hashlib.sha256(baseline_bytes).hexdigest()
        if actual_hash != targets.get(run_id):
            errors.append("legacy_activation_batch_target_hash")
    return errors


def _core_records(task_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    for name in (
        "task.json",
        "dispatch.json",
        "acceptance_fence.json",
        "terminal_output_claim.json",
    ):
        present, payload, malformed = _read_json_record(task_dir / name)
        if not present:
            continue
        if malformed or not payload:
            raise ValueError(f"{task_dir.name}: malformed {name}")
        records.append((name, payload))
    return records


def _evidence_manifest(
    task_dir: Path,
) -> tuple[list[dict[str, Any]], str, dict[str, bytes]]:
    paths = {
        "task.json": task_dir / "task.json",
        "dispatch.json": task_dir / "dispatch.json",
        "acceptance_fence.json": task_dir / "acceptance_fence.json",
        "terminal_output_claim.json": task_dir / "terminal_output_claim.json",
        "report.md": task_dir / "report.md",
        "creation_record": creation_record_path(task_dir),
    }
    manifest: list[dict[str, Any]] = []
    evidence: dict[str, bytes] = {}
    for name in _LEGACY_EVIDENCE_NAMES:
        present, raw, unsafe = read_regular_file_bytes(paths[name])
        if unsafe:
            raise ValueError(f"{task_dir.name}: unsafe {name}")
        evidence[name] = raw
        manifest.append(
            {
                "name": name,
                "present": present,
                "byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest() if present else "",
            }
        )
    encoded = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return manifest, hashlib.sha256(encoded).hexdigest(), evidence


def _decode_json_evidence(
    name: str,
    present: bool,
    raw: bytes,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    if not present:
        return False, None, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return True, None, f"malformed_{name.removesuffix('.json')}_json"
    if not isinstance(payload, dict):
        return True, None, f"malformed_{name.removesuffix('.json')}_json"
    return True, payload, None


def _excluded_task(
    task_dir: Path,
    reasons: list[str],
    manifest: list[dict[str, Any]],
    digest: str,
) -> dict[str, Any]:
    return {
        "task_id": task_dir.name,
        "reason_codes": sorted(set(reasons)),
        "evidence_manifest": manifest,
        "evidence_manifest_sha256": digest,
    }


def _normalize_legacy_run_alias(value: Any, run_id: str) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if normalized == run_id:
        return run_id
    prefix = "visible-runs/"
    if normalized.startswith(prefix) and normalized.removeprefix(prefix) == run_id:
        return run_id
    return value


def _observed_identity(
    records: list[tuple[str, dict[str, Any]]],
    *,
    run_id: str,
) -> dict[str, Any]:
    observed: dict[str, Any] = {}
    for source, payload in records:
        for field, aliases in _IDENTITY_ALIASES.items():
            values = [
                (
                    name,
                    _normalize_legacy_run_alias(payload[name], run_id)
                    if field == "run_id"
                    else payload[name],
                )
                for name in aliases
                if name in payload
            ]
            if not values:
                continue
            value = values[0][1]
            if any(alias_value != value for _, alias_value in values[1:]):
                raise ValueError(f"{source}: conflicting {field} aliases")
            if field in observed and observed[field] != value:
                raise ValueError(f"{source}: conflicting {field}")
            observed[field] = value
    return observed


def _validate_observed_identity(task_dir: Path, identity: dict[str, Any]) -> None:
    run_root = _run_root(task_dir)
    if task_dir.parent != run_root / "tasks":
        raise ValueError(f"{task_dir.name}: ambiguous task/run containment")
    if identity.get("run_id") not in {None, run_root.name}:
        raise ValueError(f"{task_dir.name}: run_id does not match task containment")
    if identity.get("task_id") not in {None, task_dir.name}:
        raise ValueError(f"{task_dir.name}: task_id does not match task containment")
    for field in (
        "run_id",
        "task_id",
        "attempt_id",
        "contract_hash",
        "worker_id",
        "session_id",
        "instance_id",
    ):
        if field in identity and (
            not isinstance(identity[field], str) or not identity[field]
        ):
            raise ValueError(f"{task_dir.name}: invalid {field}")
    for field in ("binding_generation", "fencing_generation"):
        if field in identity and (
            type(identity[field]) is not int or identity[field] < 0
        ):
            raise ValueError(f"{task_dir.name}: invalid {field}")


def _has_explicit_modern_marker(payload: dict[str, Any]) -> bool:
    return any(
        name in payload
        for name in (*PROTOCOL_ALIASES, "terminal_acceptance_required")
    )


def _validate_legacy_records(
    task_dir: Path,
    records: list[tuple[str, dict[str, Any]]],
) -> None:
    for source, payload in records:
        if source == "acceptance_fence.json" and payload.get("fence_kind") != FENCE_RECORD_KIND:
            raise ValueError(f"{task_dir.name}: malformed {source}")
        if source == "terminal_output_claim.json" and payload.get("claim_kind") not in {
            TERMINAL_OUTPUT_CLAIM_KIND,
            OPERATOR_RECOVERY_CLAIM_KIND,
        }:
            raise ValueError(f"{task_dir.name}: malformed {source}")


def _activation_metadata(
    run_root: Path,
    *,
    activation_generation: str | None,
    activation_cutoff: str | None,
) -> tuple[str, str]:
    cutoff = activation_cutoff or datetime.now(timezone.utc).isoformat()
    generation = activation_generation or (
        "legacy-baseline-"
        + hashlib.sha256(f"{run_root.name}/{cutoff}".encode("utf-8"))
        .hexdigest()[:16]
    )
    if not isinstance(generation, str) or not generation:
        raise ValueError("activation_generation must be a non-empty string")
    if not isinstance(cutoff, str) or not cutoff:
        raise ValueError("activation_cutoff must be a non-empty string")
    return generation, cutoff


def legacy_membership(task_dir: Path) -> tuple[bool, list[str]]:
    """Check whether a task belongs to a valid control-plane baseline."""
    baseline_errors: list[str] = []
    baseline_valid = False
    baseline_path = legacy_baseline_path(task_dir)
    present, baseline_bytes, unsafe = read_regular_file_bytes(baseline_path)
    baseline: dict[str, Any] = {}
    malformed = unsafe
    if present and not unsafe:
        try:
            payload = json.loads(baseline_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            malformed = True
        else:
            if isinstance(payload, dict):
                baseline = payload
            else:
                malformed = True
    if present and malformed:
        baseline_errors.append("legacy_baseline")
    if present and not malformed:
        if (
            type(baseline.get("schema_version")) is not int
            or baseline.get("schema_version") != LEGACY_BASELINE_SCHEMA_VERSION
        ):
            baseline_errors.append("legacy_baseline_schema_version")
        if baseline.get("baseline_kind") != LEGACY_BASELINE_KIND:
            baseline_errors.append("legacy_baseline_kind")
        if baseline.get("sealed") is not True:
            baseline_errors.append("legacy_baseline_sealed")
        for field in ("activation_generation", "activation_cutoff"):
            value = baseline.get(field)
            if not isinstance(value, str) or not value.strip():
                baseline_errors.append(f"legacy_baseline_{field}")
        entries = baseline.get("entries")
        if not isinstance(entries, list):
            baseline_errors.append("legacy_baseline_entries")
        else:
            run_id = _run_root(task_dir).name
            entry_ids: set[str] = set()
            for index, item in enumerate(entries):
                if not isinstance(item, dict):
                    baseline_errors.append(f"legacy_baseline_entries[{index}]")
                    continue
                entry_run = _normalize_legacy_run_alias(item.get("run_id"), run_id)
                if not isinstance(entry_run, str) or entry_run != run_id:
                    baseline_errors.append(f"legacy_baseline_entries[{index}].run_id")
                if not isinstance(item.get("task_id"), str) or not item["task_id"]:
                    baseline_errors.append(f"legacy_baseline_entries[{index}].task_id")
                elif item["task_id"] in entry_ids:
                    baseline_errors.append("legacy_baseline_entry_duplicate")
                else:
                    entry_ids.add(item["task_id"])
                if not isinstance(item.get("attempt_id"), str) or not item["attempt_id"]:
                    baseline_errors.append(f"legacy_baseline_entries[{index}].attempt_id")
                if not _is_sha256(item.get("report_sha256")):
                    baseline_errors.append(
                        f"legacy_baseline_entries[{index}].report_sha256"
                    )
                for field in (
                    "contract_hash",
                    "worker_id",
                    "session_id",
                    "instance_id",
                ):
                    if field in item and (
                        not isinstance(item[field], str) or not item[field]
                    ):
                        baseline_errors.append(f"legacy_baseline_entries[{index}].{field}")
                for field in ("binding_generation", "fencing_generation"):
                    if field in item and (
                        type(item[field]) is not int or item[field] < 0
                    ):
                        baseline_errors.append(f"legacy_baseline_entries[{index}].{field}")
            matches = [
                item
                for item in entries
                if isinstance(item, dict)
                and _normalize_legacy_run_alias(
                    item.get("run_id"), run_id
                )
                == run_id
                and item.get("task_id") == task_dir.name
            ]
            if len(matches) != 1:
                baseline_errors.append("legacy_baseline_membership")
            else:
                entry = matches[0]
                if not isinstance(entry.get("attempt_id"), str) or not entry["attempt_id"]:
                    baseline_errors.append("legacy_baseline_attempt_id")
                if (
                    not _is_sha256(entry.get("report_sha256"))
                    or entry["report_sha256"] != _report_hash(task_dir)
                ):
                    baseline_errors.append("legacy_baseline_report_sha256")
                try:
                    records = _core_records(task_dir)
                    _validate_legacy_records(task_dir, records)
                    observed = _observed_identity(
                        records,
                        run_id=_run_root(task_dir).name,
                    )
                    _validate_observed_identity(task_dir, observed)
                except ValueError:
                    baseline_errors.append("legacy_baseline_identity")
                else:
                    for field in IDENTITY_FIELDS:
                        if field in {"task_protocol_version", "terminal_acceptance_required"}:
                            continue
                        if field in entry and field in observed and observed[field] != entry[field]:
                            baseline_errors.append(f"legacy_baseline_{field}")
            excluded = baseline.get("excluded_tasks", [])
            if not isinstance(excluded, list):
                baseline_errors.append("legacy_baseline_excluded_tasks")
            else:
                excluded_ids: set[str] = set()
                for index, item in enumerate(excluded):
                    if not isinstance(item, dict):
                        baseline_errors.append(
                            f"legacy_baseline_excluded_tasks[{index}]"
                        )
                        continue
                    task_id = item.get("task_id")
                    reasons = item.get("reason_codes")
                    manifest = item.get("evidence_manifest")
                    digest = item.get("evidence_manifest_sha256")
                    if not isinstance(task_id, str) or not task_id or task_id in excluded_ids:
                        baseline_errors.append(
                            f"legacy_baseline_excluded_tasks[{index}].task_id"
                        )
                    elif task_id in entry_ids:
                        baseline_errors.append("legacy_baseline_entry_exclusion_overlap")
                    else:
                        excluded_ids.add(task_id)
                    if (
                        not isinstance(reasons, list)
                        or not reasons
                        or reasons != sorted(set(reasons))
                        or any(reason not in _EXCLUSION_REASONS for reason in reasons)
                    ):
                        baseline_errors.append(
                            f"legacy_baseline_excluded_tasks[{index}].reason_codes"
                        )
                    if not isinstance(manifest, list) or len(manifest) != len(
                        _LEGACY_EVIDENCE_NAMES
                    ):
                        baseline_errors.append(
                            f"legacy_baseline_excluded_tasks[{index}].evidence_manifest"
                        )
                    else:
                        names = [entry.get("name") for entry in manifest if isinstance(entry, dict)]
                        if names != list(_LEGACY_EVIDENCE_NAMES):
                            baseline_errors.append(
                                f"legacy_baseline_excluded_tasks[{index}].evidence_manifest"
                            )
                        for manifest_entry in manifest:
                            if not isinstance(manifest_entry, dict):
                                continue
                            present = manifest_entry.get("present")
                            byte_length = manifest_entry.get("byte_length")
                            sha256 = manifest_entry.get("sha256")
                            if type(present) is not bool or type(byte_length) is not int or byte_length < 0:
                                baseline_errors.append(
                                    f"legacy_baseline_excluded_tasks[{index}].evidence_manifest"
                                )
                            if not present and byte_length != 0:
                                baseline_errors.append(
                                    f"legacy_baseline_excluded_tasks[{index}].evidence_manifest"
                                )
                            if not isinstance(sha256, str) or (
                                present and not _is_sha256(sha256)
                            ) or (not present and sha256):
                                baseline_errors.append(
                                    f"legacy_baseline_excluded_tasks[{index}].evidence_manifest"
                                )
                        encoded = json.dumps(
                            manifest,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                        if digest != hashlib.sha256(encoded).hexdigest():
                            baseline_errors.append(
                                f"legacy_baseline_excluded_tasks[{index}].evidence_manifest_sha256"
                            )
            baseline_errors.extend(
                _activation_batch_errors(
                    task_dir,
                    baseline,
                    baseline_bytes,
                )
            )
        baseline_valid = not baseline_errors
    if baseline_valid:
        return True, []
    return False, sorted(set(baseline_errors))


def build_legacy_terminal_baseline(
    run_root: Path,
    *,
    activation_generation: str | None = None,
    activation_cutoff: str | None = None,
    activation_batch_id: str | None = None,
    allow_exclusions: bool = False,
) -> dict[str, Any]:
    """Build a sealed inventory of pre-v2 task identities before activation."""
    generation, cutoff = _activation_metadata(
        run_root,
        activation_generation=activation_generation,
        activation_cutoff=activation_cutoff,
    )
    entries: list[dict[str, Any]] = []
    excluded_tasks: list[dict[str, Any]] = []
    tasks_root = run_root / "tasks"
    try:
        tasks_mode = tasks_root.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{run_root.name}: tasks directory missing") from exc
    if stat.S_ISLNK(tasks_mode) or not stat.S_ISDIR(tasks_mode):
        raise ValueError(f"{run_root.name}: tasks directory is not a real directory")
    run_root_resolved = run_root.resolve()
    for task_dir in sorted(tasks_root.iterdir()):
        try:
            task_mode = task_dir.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"{run_root.name}: unreadable task inventory entry") from exc
        if stat.S_ISLNK(task_mode) or not stat.S_ISDIR(task_mode):
            raise ValueError(f"{task_dir.name}: invalid task inventory entry")
        try:
            task_dir.resolve().relative_to(run_root_resolved)
        except ValueError as exc:
            raise ValueError(f"{task_dir.name}: task escapes run root") from exc
        manifest, manifest_digest, evidence = _evidence_manifest(task_dir)
        decoded: dict[str, dict[str, Any]] = {}
        reasons: list[str] = []
        for name in _CORE_RECORD_NAMES:
            present, payload, reason = _decode_json_evidence(
                name,
                next(
                    item["present"]
                    for item in manifest
                    if item["name"] == name
                ),
                evidence[name],
            )
            if reason:
                reasons.append(reason)
            elif present and payload is not None:
                decoded[name] = payload
        creation_present, creation, creation_reason = _decode_json_evidence(
            "creation_record",
            next(
                item["present"]
                for item in manifest
                if item["name"] == "creation_record"
            ),
            evidence["creation_record"],
        )
        if creation_present and creation is not None:
            continue
        if creation_reason:
            reasons.append(creation_reason)
        records = [(name, decoded[name]) for name in _CORE_RECORD_NAMES if name in decoded]
        if any(_has_explicit_modern_marker(payload) for _, payload in records):
            continue
        report_present = next(
            item["present"] for item in manifest if item["name"] == "report.md"
        )
        report = evidence["report.md"]
        if not report_present:
            reasons.append("missing_report")
        elif not report.strip():
            reasons.append("empty_report")
        try:
            _validate_legacy_records(task_dir, records)
        except ValueError:
            reasons.append("invalid_legacy_record")
        try:
            observed = _observed_identity(records, run_id=run_root.name)
            _validate_observed_identity(task_dir, observed)
        except ValueError:
            observed = {}
            reasons.append("invalid_legacy_identity")
        if reasons:
            if not allow_exclusions:
                raise ValueError(f"{task_dir.name}: {'; '.join(sorted(set(reasons)))}")
            invalid_reasons = set(reasons) - _EXCLUSION_REASONS
            if invalid_reasons:
                raise ValueError(
                    f"{task_dir.name}: unsupported exclusion reasons: {sorted(invalid_reasons)}"
                )
            excluded_tasks.append(
                _excluded_task(task_dir, reasons, manifest, manifest_digest)
            )
            continue
        report_hash = hashlib.sha256(report).hexdigest()
        entry: dict[str, Any] = {
            "run_id": run_root.name,
            "task_id": task_dir.name,
            "attempt_id": observed.get("attempt_id")
            or "legacy-"
            + hashlib.sha256(
                f"{run_root.name}/{task_dir.name}/{report_hash}".encode("utf-8")
            ).hexdigest()[:16],
            "report_sha256": report_hash,
        }
        for field in (
            "contract_hash",
            "worker_id",
            "session_id",
            "instance_id",
            "binding_generation",
            "fencing_generation",
        ):
            if field in observed:
                entry[field] = observed[field]
        entries.append(entry)
    baseline = {
        "schema_version": LEGACY_BASELINE_SCHEMA_VERSION,
        "baseline_kind": LEGACY_BASELINE_KIND,
        "sealed": True,
        "activation_generation": generation,
        "activation_cutoff": cutoff,
        "entries": entries,
        "excluded_tasks": excluded_tasks,
    }
    if activation_batch_id is not None:
        if not _is_sha256(activation_batch_id):
            raise ValueError("activation_batch_id must be a SHA-256 digest")
        baseline["activation_batch_id"] = activation_batch_id
        baseline["activation_scope"] = "all_runs_batch"
    return baseline
