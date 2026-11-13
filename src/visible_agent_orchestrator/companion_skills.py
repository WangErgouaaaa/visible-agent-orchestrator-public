"""Read-only validation and status for the fixed Companion Skill allowlist."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from visible_agent_orchestrator.terminal_protocol_baseline import (
    read_regular_file_bytes,
)


MANAGED_SKILL_IDS = (
    "visible-agent-orchestrator",
    "ag2-meeting",
)
MANAGED_SKILL_LAYOUT = {
    "visible-agent-orchestrator": (
        "companion-skill-sources/visible-agent-orchestrator/SKILL.md",
        ("plugins/visible-agent-orchestrator-skill-v0/SKILL.md",),
        "codex-user",
        "visible-agent-orchestrator",
        "0.3.0",
        "visible-agent-orchestrator-runtime-v1",
    ),
    "ag2-meeting": (
        "companion-skill-sources/ag2-meeting/SKILL.md",
        (
            ".agents/skills/ag2-meeting/SKILL.md",
            "plugins/ag2-meeting-runtime/skills/ag2-meeting/SKILL.md",
        ),
        "agents-user",
        "ag2-meeting",
        "1.0.0",
        "visible-agent-orchestrator-runtime-v1",
    ),
}
STATUS_VALUES = {"current", "drifted", "missing", "invalid", "unverified"}
OVERALL_VALUES = {"current", "degraded", "invalid", "unverified"}
REVIEW_DISPOSITIONS = ("affected", "reviewed-no-change", "not-applicable")
REASON_REQUIRED_FOR = ("reviewed-no-change", "not-applicable")
_CONTRACT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SEMANTIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+\Z")
_PACKAGE_MAX_BYTES = 16 * 1024 * 1024
_GENERATION_MANIFEST_BASENAME = ".generation-manifest.json"
_INSTALLED_WRITE_BITS = 0o222
_INSTALLED_ROOT_MODE = 0o500


class RegistryError(ValueError):
    """The managed registry is malformed or reaches an unsafe path."""


@dataclass(frozen=True)
class CompanionSkill:
    skill_id: str
    canonical_package: str
    canonical_source: str
    discovery_paths: tuple[str, ...]
    install_scope: str
    active_name: str
    contract_version: str
    runtime_identity: str
    impact_path_prefixes: tuple[str, ...]
    impact_contracts: tuple[str, ...]


@dataclass(frozen=True)
class CompanionSkillRegistry:
    skills: tuple[CompanionSkill, ...]


def _canonical_string(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or unicodedata.normalize("NFC", value) != value
    ):
        raise RegistryError(f"{field} must be a non-empty NFC string")
    return value


def _safe_relative_path(value: object, field: str) -> str:
    value = _canonical_string(value, field)
    path = Path(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or path.as_posix() != value
    ):
        raise RegistryError(f"{field} must be a canonical relative path: {value}")
    return value


def _contract_name(value: object, field: str) -> str:
    value = _canonical_string(value, field)
    if _CONTRACT_NAME.fullmatch(value) is None:
        raise RegistryError(f"{field} must be a canonical lowercase contract name")
    return value


def _decode_registry(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RegistryError("registry unreadable or invalid") from exc
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise RegistryError("registry schema_version must be 1")
    return payload


def _read_registry(path: Path) -> dict[str, Any]:
    present, raw, unsafe = read_regular_file_bytes(path, max_bytes=_PACKAGE_MAX_BYTES)
    if not present or unsafe:
        raise RegistryError(f"registry missing or unsafe: {path}")
    return _decode_registry(raw)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise RegistryError("registry contains duplicate key")
        payload[key] = value
    return payload


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {token}")


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _git_output(source: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(source), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RegistryError("approved Git object is unavailable")
    return result.stdout


def skill_install_target(skill: CompanionSkill) -> Path:
    home = os.environ.get("HOME") or str(Path.home())
    raw_root = (
        os.environ.get("CODEX_HOME") or f"{home}/.codex"
        if skill.install_scope == "codex-user"
        else f"{home}/.agents"
    )
    if (
        not raw_root
        or unicodedata.normalize("NFC", raw_root) != raw_root
        or not Path(raw_root).is_absolute()
        or raw_root != Path(raw_root).as_posix()
        or "." in Path(raw_root).parts
        or ".." in Path(raw_root).parts
    ):
        raise RegistryError("target root must be absolute, NFC, and lexically normalized")
    return Path(raw_root) / "skills" / skill.active_name


def load_committed_registry(
    source: Path,
    commit: str,
    root: Path,
) -> tuple[CompanionSkillRegistry, str]:
    if not source.is_absolute() or not root.is_absolute() or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise RegistryError("release source, registry root, and commit must be canonical")
    registry_bytes = _git_output(source, "show", f"{commit}:contracts/companion-skills.registry.json")
    payload = _decode_registry(registry_bytes)
    return _load_registry_payload(payload, root), "sha256:" + hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def assemble_release_plan(
    registry: CompanionSkillRegistry,
    source_commit: str,
    registry_digest: str,
    manifests: Mapping[str, Mapping[str, object]],
    targets: Mapping[str, Path],
) -> Mapping[str, object]:
    if set(manifests) != set(MANAGED_SKILL_IDS) or set(targets) != set(MANAGED_SKILL_IDS):
        raise RegistryError("release inputs must close the fixed Skill inventory")
    skills = [
        {
            "skill_id": skill.skill_id,
            "contract_version": skill.contract_version,
            "canonical_package": skill.canonical_package,
            "tree_hash": manifests[skill.skill_id]["tree_hash"],
            "generation_id": manifests[skill.skill_id]["generation_id"],
            "target": str(targets[skill.skill_id]),
            "required_runtime_identity": skill.runtime_identity,
            "observed_runtime_identity": None,
            "runtime_compatibility": "unknown",
            "manifest": manifests[skill.skill_id],
        }
        for skill in registry.skills
    ]
    runtime_identity = registry.skills[0].runtime_identity
    if any(skill.runtime_identity != runtime_identity for skill in registry.skills):
        raise RegistryError("release registry has mixed runtime compatibility")
    closure: Mapping[str, object] = {
        "schema_version": 1,
        "digest_protocol": "sha256-canonical-json-v1",
        "source_commit": source_commit,
        "registry_digest": registry_digest,
        "contract_generation": 1,
        "required_runtime_identity": runtime_identity,
        "observed_runtime_identity": None,
        "runtime_compatibility": "unknown",
        "runtime_attestation": False,
        "skills": skills,
    }
    release_id = hashlib.sha256(_canonical_json_bytes(closure)).hexdigest()
    return {**closure, "release_id": release_id, "release_digest": "sha256:" + release_id}


def load_registry(path: Path, root: Path) -> CompanionSkillRegistry:
    """Load the four fixed identities without accepting caller-defined additions."""
    payload = _read_registry(path)
    return _load_registry_payload(payload, root)


def _load_registry_payload(payload: Mapping[str, Any], root: Path) -> CompanionSkillRegistry:
    if set(payload) != {"schema_version", "purpose", "impact_protocol", "release_protocol", "skills"}:
        raise RegistryError("registry fields must be canonical")
    _canonical_string(payload.get("purpose"), "purpose")
    protocol = payload.get("impact_protocol")
    if protocol != {
        "is_complete_inventory": False,
        "non_match_result": "unverified",
        "reason_required_for": list(REASON_REQUIRED_FOR),
        "review_dispositions": list(REVIEW_DISPOSITIONS),
    }:
        raise RegistryError("impact_protocol must use the canonical fail-closed policy")
    release_protocol = payload.get("release_protocol")
    if (
        not isinstance(release_protocol, dict)
        or type(release_protocol.get("schema_version")) is not int
        or type(release_protocol.get("contract_generation")) is not int
        or release_protocol != {
        "schema_version": 1,
        "digest_protocol": "sha256-canonical-json-v1",
        "contract_generation": 1,
        "runtime_identity": "visible-agent-orchestrator-runtime-v1",
        }
    ):
        raise RegistryError("release_protocol must use the canonical fixed policy")
    entries = payload.get("skills")
    if not isinstance(entries, list) or len(entries) != len(MANAGED_SKILL_IDS):
        raise RegistryError(
            f"registry must contain exactly {len(MANAGED_SKILL_IDS)} managed skills"
        )
    skills: list[CompanionSkill] = []
    discovery_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryError("registry skill entry must be an object")
        if set(entry) != {
            "id",
            "canonical_package",
            "canonical_source",
            "discovery_paths",
            "install_target",
            "contract_version",
            "runtime_compatibility",
            "impact_triggers",
        }:
            raise RegistryError("registry skill fields must be canonical")
        skill_id = entry.get("id")
        if skill_id not in MANAGED_SKILL_IDS:
            raise RegistryError(f"canonical skill id required: {skill_id}")
        canonical_package = _safe_relative_path(entry.get("canonical_package"), "canonical_package")
        canonical_source = _safe_relative_path(
            entry.get("canonical_source"), "canonical_source"
        )
        if (
            not canonical_package.startswith("companion-skill-sources/")
            or canonical_source != f"{canonical_package}/SKILL.md"
        ):
            raise RegistryError("canonical source must be non-discoverable")
        raw_discovery = entry.get("discovery_paths")
        if not isinstance(raw_discovery, list) or not raw_discovery:
            raise RegistryError("discovery_paths must be a non-empty list")
        normalized_discovery = tuple(
            _safe_relative_path(item, "discovery_path") for item in raw_discovery
        )
        if canonical_source in normalized_discovery:
            raise RegistryError("canonical source must not be a discovery surface")
        for discovery_path in normalized_discovery:
            if discovery_path in discovery_paths:
                raise RegistryError(f"discovery path reused: {discovery_path}")
            discovery_paths.add(discovery_path)

        install = entry.get("install_target")
        if not isinstance(install, dict) or set(install) != {"scope", "active_name"}:
            raise RegistryError("install_target must be an object")
        install_scope = _canonical_string(install.get("scope"), "install scope")
        active_name = _canonical_string(install.get("active_name"), "active name")
        if "/" in active_name or active_name in {".", ".."}:
            raise RegistryError("active name must be one path component")
        contract_version = _canonical_string(entry.get("contract_version"), "contract version")
        if _SEMANTIC_VERSION.fullmatch(contract_version) is None:
            raise RegistryError("contract version must be numeric dotted components")
        runtime = entry.get("runtime_compatibility")
        if not isinstance(runtime, dict) or set(runtime) != {"required_identity"}:
            raise RegistryError("runtime_compatibility must have required_identity")
        runtime_identity = _canonical_string(runtime.get("required_identity"), "runtime identity")

        triggers = entry.get("impact_triggers")
        if not isinstance(triggers, dict) or set(triggers) != {"path_prefixes", "contracts"}:
            raise RegistryError("impact_triggers must be an object")
        raw_prefixes = triggers.get("path_prefixes")
        raw_contracts = triggers.get("contracts")
        if (
            not isinstance(raw_prefixes, list)
            or not raw_prefixes
            or not isinstance(raw_contracts, list)
            or not raw_contracts
        ):
            raise RegistryError("impact_triggers must declare paths and contracts")
        prefixes = tuple(
            _safe_relative_path(item, "impact path prefix") for item in raw_prefixes
        )
        contracts = tuple(
            _contract_name(item, "impact contract") for item in raw_contracts
        )
        if len(prefixes) != len(set(prefixes)) or len(contracts) != len(set(contracts)):
            raise RegistryError("impact triggers must not contain duplicates")

        expected_source, expected_discovery, expected_scope, expected_name, expected_version, expected_runtime = (
            MANAGED_SKILL_LAYOUT[skill_id]
        )
        if (
            canonical_source != expected_source
            or normalized_discovery != expected_discovery
            or install_scope != expected_scope
            or active_name != expected_name
            or contract_version != expected_version
            or runtime_identity != expected_runtime
        ):
            raise RegistryError(f"managed skill layout must be canonical: {skill_id}")
        skills.append(
            CompanionSkill(
                skill_id=skill_id,
                canonical_package=canonical_package,
                canonical_source=canonical_source,
                discovery_paths=normalized_discovery,
                install_scope=install_scope,
                active_name=active_name,
                contract_version=contract_version,
                runtime_identity=runtime_identity,
                impact_path_prefixes=prefixes,
                impact_contracts=contracts,
            )
        )
    if tuple(skill.skill_id for skill in skills) != MANAGED_SKILL_IDS:
        raise RegistryError("registry skill order and identities must be canonical")
    if not root.is_absolute():
        raise RegistryError("repository root must be absolute")
    return CompanionSkillRegistry(skills=tuple(skills))


def _artifact_evidence(
    path: Path, expected_sha256: str | None = None
) -> tuple[str, str | None]:
    present, raw, unsafe = read_regular_file_bytes(path, max_bytes=_PACKAGE_MAX_BYTES)
    if not present:
        return "missing", None
    if unsafe:
        return "invalid", None
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        return "drifted", digest
    return "current", digest


def _skill_source_evidence(path: Path, skill_id: str) -> tuple[str, str | None]:
    present, raw, unsafe = read_regular_file_bytes(path, max_bytes=_PACKAGE_MAX_BYTES)
    if not present:
        return "missing", None
    if unsafe or not raw.startswith(f"---\nname: {skill_id}\n".encode()):
        return "invalid", None
    return "current", hashlib.sha256(raw).hexdigest()


def _path_matches(changed_path: str, prefix: str) -> bool:
    return changed_path == prefix or changed_path.startswith(prefix.rstrip("/") + "/")


def known_impact_lower_bound(
    skill: CompanionSkill,
    *,
    changed_paths: Sequence[str] = (),
    changed_contracts: Sequence[str] = (),
) -> dict[str, object]:
    """Return definite lower-bound matches; a miss remains unverified."""
    paths = tuple(_safe_relative_path(path, "changed path") for path in changed_paths)
    contracts = tuple(
        _contract_name(contract, "changed contract") for contract in changed_contracts
    )
    path_matches = sorted(
        {
            prefix
            for prefix in skill.impact_path_prefixes
            if any(_path_matches(path, prefix) for path in paths)
        }
    )
    contract_matches = sorted(set(contracts).intersection(skill.impact_contracts))
    affected = bool(path_matches or contract_matches)
    return {
        "skill_id": skill.skill_id,
        "status": "affected" if affected else "unverified",
        "path_matches": path_matches,
        "contract_matches": contract_matches,
        "review_dispositions": list(REVIEW_DISPOSITIONS),
        "reason_required_for": list(REASON_REQUIRED_FOR),
        "is_complete_impact_inventory": False,
    }


def load_impact_declaration(path: Path) -> dict[str, Any]:
    present, raw, unsafe = read_regular_file_bytes(path, max_bytes=_PACKAGE_MAX_BYTES)
    if not present or unsafe:
        raise RegistryError(f"impact declaration missing or unsafe: {path}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"impact declaration unreadable or invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise RegistryError("impact declaration must be an object")
    return payload


def validate_impact_declaration(
    registry: CompanionSkillRegistry,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Validate one persistent review against remaining managed Skills."""
    if payload.get("schema_version") != 1:
        raise RegistryError("impact declaration schema_version must be 1")
    change_id = _canonical_string(payload.get("change_id"), "change_id")
    raw_paths = payload.get("changed_paths")
    raw_contracts = payload.get("changed_contracts")
    raw_reviews = payload.get("skills")
    if (
        not isinstance(raw_paths, list)
        or not isinstance(raw_contracts, list)
        or not isinstance(raw_reviews, list)
    ):
        raise RegistryError("impact declaration paths, contracts, and skills are required")
    paths = tuple(_safe_relative_path(path, "changed path") for path in raw_paths)
    contracts = tuple(
        _contract_name(contract, "changed contract") for contract in raw_contracts
    )
    if not paths and not contracts:
        raise RegistryError("impact declaration must identify a changed path or contract")
    if len(raw_reviews) != len(registry.skills):
        raise RegistryError("impact declaration must review all managed skills")

    results: list[dict[str, object]] = []
    for skill, review in zip(registry.skills, raw_reviews):
        if not isinstance(review, dict) or review.get("skill_id") != skill.skill_id:
            raise RegistryError("impact declaration skill order and identities must be canonical")
        disposition = review.get("disposition")
        if disposition not in REVIEW_DISPOSITIONS:
            raise RegistryError(f"invalid impact disposition for {skill.skill_id}")
        reason = review.get("reason")
        if disposition in REASON_REQUIRED_FOR and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise RegistryError(f"impact reason required for {skill.skill_id}")
        lower_bound = known_impact_lower_bound(
            skill,
            changed_paths=paths,
            changed_contracts=contracts,
        )
        if lower_bound["status"] == "affected" and disposition == "not-applicable":
            raise RegistryError(
                f"known impact cannot be declared not-applicable: {skill.skill_id}"
            )
        results.append(
            {
                **lower_bound,
                "disposition": disposition,
                "reason": reason if isinstance(reason, str) else "",
            }
        )
    return {
        "schema_version": 1,
        "read_only": True,
        "change_id": change_id,
        "valid": True,
        "skills": results,
    }


def _summary(values: Sequence[str]) -> str:
    for value in ("invalid", "drifted", "missing", "unverified"):
        if value in values:
            return value
    return "current"


@dataclass(frozen=True)
class _PackageTree:
    """Package identity the installer's generation manifest records per tree."""

    root_mode: int
    directories: dict[str, int]
    files: dict[str, tuple[str, int]]


_EMPTY_PACKAGE_TREE = _PackageTree(0, {}, {})


def _package_inventory(path: Path) -> tuple[str, _PackageTree]:
    try:
        root = path.lstat()
    except FileNotFoundError:
        return "missing", _EMPTY_PACKAGE_TREE
    if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
        return "invalid", _EMPTY_PACKAGE_TREE
    directories: dict[str, int] = {}
    files: dict[str, tuple[str, int]] = {}
    total_bytes = 0
    scan_error = False

    def mark_scan_error(_error: OSError) -> None:
        nonlocal scan_error
        scan_error = True

    for current, directory_names, file_names in os.walk(path, followlinks=False, onerror=mark_scan_error):
        if scan_error:
            return "invalid", _EMPTY_PACKAGE_TREE
        current_path = Path(current)
        for directory in directory_names:
            entry = current_path / directory
            try:
                observed_directory = entry.lstat()
            except OSError:
                return "invalid", _EMPTY_PACKAGE_TREE
            if stat.S_ISLNK(observed_directory.st_mode) or not stat.S_ISDIR(observed_directory.st_mode):
                return "invalid", _EMPTY_PACKAGE_TREE
            directories[entry.relative_to(path).as_posix()] = stat.S_IMODE(
                observed_directory.st_mode
            )
        for filename in file_names:
            candidate = current_path / filename
            try:
                before = candidate.lstat()
            except OSError:
                return "invalid", _EMPTY_PACKAGE_TREE
            if not stat.S_ISREG(before.st_mode):
                return "invalid", _EMPTY_PACKAGE_TREE
            present, raw, unsafe = read_regular_file_bytes(candidate, max_bytes=_PACKAGE_MAX_BYTES)
            if not present or unsafe:
                return "invalid", _EMPTY_PACKAGE_TREE
            total_bytes += len(raw)
            if total_bytes > _PACKAGE_MAX_BYTES:
                return "invalid", _EMPTY_PACKAGE_TREE
            try:
                after = candidate.lstat()
            except OSError:
                return "invalid", _EMPTY_PACKAGE_TREE
            if (
                not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode))
                != (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode))
            ):
                return "invalid", _EMPTY_PACKAGE_TREE
            relative = candidate.relative_to(path).as_posix()
            files[relative] = (
                hashlib.sha256(raw).hexdigest(),
                stat.S_IMODE(before.st_mode),
            )
    if scan_error:
        return "invalid", _EMPTY_PACKAGE_TREE
    return "current", _PackageTree(stat.S_IMODE(root.st_mode), directories, files)


def _package_status(canonical: Path, derived: Path) -> str:
    source_status, source_tree = _package_inventory(canonical)
    derived_status, derived_tree = _package_inventory(derived)
    if source_status != "current":
        return source_status
    if derived_status != "current":
        return derived_status
    return "current" if source_tree.files == derived_tree.files else "drifted"


def _installed_package_status(canonical: Path, installed: Path) -> str:
    """Compare the installed package root against the managed canonical package.

    The installed root is either an active immutable generation directory or
    the install target. Three narrow accommodations keep supported generations
    comparable: the installer-reserved generation manifest basename is not a
    managed package file, installed files and directories seal write bits off
    the canonical source modes, and the sealed generation root carries the
    installer's fixed installed root mode instead of the canonical root mode.
    """
    installed_status, installed_tree = _package_inventory(installed)
    if installed_status != "current":
        return installed_status
    canonical_status, canonical_tree = _package_inventory(canonical)
    if canonical_status != "current":
        return "unverified"
    expected_files = {
        relative: (digest, mode & ~_INSTALLED_WRITE_BITS)
        for relative, (digest, mode) in canonical_tree.files.items()
    }
    observed_files = {
        relative: entry
        for relative, entry in installed_tree.files.items()
        if relative != _GENERATION_MANIFEST_BASENAME
    }
    expected_directories = {
        relative: mode & ~_INSTALLED_WRITE_BITS
        for relative, mode in canonical_tree.directories.items()
    }
    current = (
        observed_files == expected_files
        and installed_tree.directories == expected_directories
        and installed_tree.root_mode == _INSTALLED_ROOT_MODE
    )
    return "current" if current else "drifted"


def status_for_registry(
    registry: CompanionSkillRegistry,
    root: Path,
    *,
    installed_paths: Mapping[str, Path] | None = None,
    active_statuses: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Report independent dimensions without installation, activation, or mutation.

    installed_paths values are installed package roots: an active immutable
    generation directory or the install target itself, compared against the
    whole managed canonical package.
    """
    installed_paths = installed_paths or {}
    active_statuses = active_statuses or {}
    skills: list[dict[str, object]] = []
    observed_dimensions: list[str] = []
    for skill in registry.skills:
        canonical = root / skill.canonical_source
        source, canonical_sha = _skill_source_evidence(canonical, skill.skill_id)
        package = _summary(
            [_package_status(root / skill.canonical_package, (root / path).parent) for path in skill.discovery_paths]
        )
        discovery_details = [
            {
                "path": path,
                "status": _artifact_evidence(root / path, canonical_sha)[0],
            }
            for path in skill.discovery_paths
        ]
        discovery_values = [str(item["status"]) for item in discovery_details]
        discovery = (
            _summary(discovery_values) if source == "current" else "unverified"
        )
        installed_path = installed_paths.get(skill.skill_id)
        installed = (
            _installed_package_status(root / skill.canonical_package, installed_path)
            if installed_path is not None and source == "current"
            else "unverified"
        )
        active = active_statuses.get(skill.skill_id, "unverified")
        if active not in STATUS_VALUES:
            raise RegistryError(f"invalid active status for {skill.skill_id}: {active}")
        dimensions = {
            "source": source,
            "package": package,
            "installed": installed,
            "active": active,
            "discovery": discovery,
            "compatibility": "unverified",
            "session": "unverified",
        }
        findings = [
            {"dimension": dimension, "status": status}
            for dimension, status in dimensions.items()
            if status != "current"
        ]
        findings.extend(
            {
                "dimension": "discovery",
                "path": str(item["path"]),
                "status": str(item["status"]),
            }
            for item in discovery_details
            if item["status"] != "current"
        )
        if installed_path is not None and installed != "current":
            findings.append(
                {
                    "dimension": "installed",
                    "path": str(installed_path),
                    "status": installed,
                }
            )
        observed_dimensions.extend(dimensions.values())
        skills.append(
            {
                "skill_id": skill.skill_id,
                **dimensions,
                "install_target": {
                    "scope": skill.install_scope,
                    "active_name": skill.active_name,
                    "observed_path": (
                        str(installed_path) if installed_path is not None else None
                    ),
                },
                "discovery_details": discovery_details,
                "findings": findings,
            }
        )
    if "invalid" in observed_dimensions:
        overall = "invalid"
    elif "drifted" in observed_dimensions or "missing" in observed_dimensions:
        overall = "degraded"
    elif "unverified" in observed_dimensions:
        overall = "unverified"
    else:
        overall = "current"
    assert overall in OVERALL_VALUES
    return {
        "schema_version": 1,
        "read_only": True,
        "overall": overall,
        "skills": skills,
    }
