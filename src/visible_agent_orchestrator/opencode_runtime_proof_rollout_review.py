from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REVIEW_KIND = "opencode-runtime-proof-rollout-review"
REVIEW_STATE = "review_only"
REVIEW_MODE = "no-apply"
PHASE_RANGE = [15, 16, 17, 18, 19]

CLI_HOST = "bin/omx_opencode_raw_capture_readiness"
CLI_COMMANDS = [
    "runtime-proof-authorization-package",
    "validate-runtime-proof-authorization-package",
    "runtime-proof-runner-plan",
    "validate-runtime-proof-runner-plan",
    "runtime-proof-execution-wrapper",
    "validate-runtime-proof-execution-wrapper",
    "runtime-proof-postcheck-contract",
    "validate-runtime-proof-postcheck-contract",
    "runtime-proof-executor-contract",
    "validate-runtime-proof-executor-contract",
]

SOURCE_MODULES = [
    "src/visible_agent_orchestrator/opencode_runtime_proof_authorization.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_runner_plan.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_execution_wrapper.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_postcheck_contract.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_executor_contract.py",
]
SCHEMA_ASSETS = [
    "worker-backends/opencode-runtime-proof-authorization-package.schema.json",
    "worker-backends/opencode-runtime-proof-runner-plan.schema.json",
    "worker-backends/opencode-runtime-proof-execution-wrapper.schema.json",
    "worker-backends/opencode-runtime-proof-postcheck-contract.schema.json",
    "worker-backends/opencode-runtime-proof-executor-contract.schema.json",
]
EXAMPLES_ASSETS = [
    "worker-backends/opencode-runtime-proof-authorization-package.examples.md",
    "worker-backends/opencode-runtime-proof-runner-plan.examples.md",
    "worker-backends/opencode-runtime-proof-execution-wrapper.examples.md",
    "worker-backends/opencode-runtime-proof-postcheck-contract.examples.md",
    "worker-backends/opencode-runtime-proof-executor-contract.examples.md",
]
DOC_EVIDENCE = [
    "README.md",
    "docs/INSTALL_SYNC_README.md",
    "install/visible-agent-orchestrator.install-manifest.json",
    "multi-agent_questions/closed_questions/closed_task-status_opencode-subagent-observability-board.md",
]
REQUIRED_ROLLOUT_ASSETS = [CLI_HOST, *SOURCE_MODULES, *SCHEMA_ASSETS, *EXAMPLES_ASSETS, *DOC_EVIDENCE]

NO_APPLY_FLAGS = [
    "would_apply_install_sync",
    "would_write_data_sync",
    "would_execute_opencode",
    "would_load_plugin",
    "would_create_temp_root",
    "would_run_cleanup",
    "would_write_cleanup_proof",
    "would_write_task_local",
    "would_write_registry",
    "would_write_state_team",
    "would_touch_tmux",
    "would_call_tui",
]
CLAIM_FLAGS = [
    "claims_installed_runtime_proof",
    "claims_runtime_execution_proven",
]


def build_runtime_proof_rollout_review(
    *,
    review_id: str,
    source_root: str | Path,
    target_root: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    source = Path(source_root).resolve()
    target = Path(target_root)
    manifest = Path(manifest_path)
    manifest_payload = _read_manifest(manifest)
    manifest_entries = _manifest_entries_by_source(manifest_payload)
    package_glob = _package_glob_entry(manifest_payload)
    install_sync_text = _read_text(source / "bin" / "omx_install_sync")

    inventory = [
        _build_asset(
            source,
            target,
            source_rel=CLI_HOST,
            asset_kind="bin_script",
            phase=15,
            manifest_entries=manifest_entries,
            package_glob=package_glob,
            install_sync_text=install_sync_text,
        )
    ]
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=rel,
            asset_kind="python_module",
            phase=phase,
            manifest_entries=manifest_entries,
            package_glob=package_glob,
            install_sync_text=install_sync_text,
        )
        for phase, rel in zip(PHASE_RANGE, SOURCE_MODULES, strict=True)
    )
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=rel,
            asset_kind="worker_backend_schema",
            phase=phase,
            manifest_entries=manifest_entries,
            package_glob=package_glob,
            install_sync_text=install_sync_text,
        )
        for phase, rel in zip(PHASE_RANGE, SCHEMA_ASSETS, strict=True)
    )
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=rel,
            asset_kind="worker_backend_examples",
            phase=phase,
            manifest_entries=manifest_entries,
            package_glob=package_glob,
            install_sync_text=install_sync_text,
        )
        for phase, rel in zip(PHASE_RANGE, EXAMPLES_ASSETS, strict=True)
    )
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=rel,
            asset_kind="review_evidence_doc",
            phase=20,
            manifest_entries=manifest_entries,
            package_glob=package_glob,
            install_sync_text=install_sync_text,
        )
        for rel in DOC_EVIDENCE
    )

    review = {
        "schema_version": SCHEMA_VERSION,
        "review_kind": REVIEW_KIND,
        "review_id": str(review_id),
        "review_state": REVIEW_STATE,
        "review_mode": REVIEW_MODE,
        "source_root": str(source),
        "target_root": str(target),
        "manifest_path": str(manifest),
        "phase_range": list(PHASE_RANGE),
        "asset_inventory": inventory,
        "cli_commands": list(CLI_COMMANDS),
        "source_modules": list(SOURCE_MODULES),
        "schema_assets": list(SCHEMA_ASSETS),
        "examples_assets": list(EXAMPLES_ASSETS),
        "readme_anchors": _readme_anchors(source / "README.md"),
        "install_manifest_bindings": _manifest_bindings(inventory),
        "install_allowlist_bindings": _allowlist_bindings(inventory),
        "rollout_readiness_summary": _rollout_summary(inventory),
        "no_apply_flags": {key: False for key in NO_APPLY_FLAGS},
        "claims_installed_runtime_proof": False,
        "claims_runtime_execution_proven": False,
        "safe_next_actions": [
            "review this runtime-proof rollout package as source-only",
            "resolve any manifest or allowlist drift before requesting install/sync apply",
            "request separate explicit user authorization before any future install/sync apply or runtime proof",
        ],
        "forbidden_next_actions": [
            "do not run install/sync apply from this rollout review package",
            "do not write the selected runtime root from this rollout review package",
            "do not execute OpenCode, load plugins, create temp roots, run cleanup, or write cleanup proof",
            "do not write task-local protocol markers, registries, .omx/state/team, tmux, or /tui",
            "do not claim installed/runtime proof from this source-only review package",
        ],
        "advisory": (
            "Source-only no-apply runtime-proof rollout review package; it inventories Phase 15-19 "
            "CLI/module/schema/docs assets and validates source metadata only. Future install/sync apply "
            "still needs separate explicit authorization."
        ),
    }
    for key in NO_APPLY_FLAGS:
        review[key] = False
    review["validation"] = validate_runtime_proof_rollout_review(review)
    review["blocked_reasons"] = list(review["validation"]["errors"])
    review["warnings"] = list(review["validation"]["warnings"])
    review["rollout_readiness_summary"] = _rollout_summary(inventory, validation_ok=review["validation"]["ok"])
    return review


def validate_runtime_proof_rollout_review(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof rollout review validation is source-only/no-apply",
        "Validation does not run install/sync apply, write the selected runtime root, execute OpenCode, or touch workers",
    ]

    if payload.get("review_kind") != REVIEW_KIND:
        errors.append(f"review_kind must be {REVIEW_KIND}")
    if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(payload.get("review_id", "")):
        errors.append("review_id missing")
    if payload.get("review_state") != REVIEW_STATE:
        errors.append("review_state must be review_only")
    if payload.get("review_mode") != REVIEW_MODE:
        errors.append("review_mode must be no-apply")
    if _list_int(payload.get("phase_range")) != PHASE_RANGE:
        errors.append("phase_range must be [15, 16, 17, 18, 19]")

    no_apply_flags = _dict(payload.get("no_apply_flags"))
    for key in NO_APPLY_FLAGS:
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")
        if bool(no_apply_flags.get(key, False)):
            errors.append(f"no_apply_flags.{key} must be false")
    for key in CLAIM_FLAGS:
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    inventory = [_dict(item) for item in _list(payload.get("asset_inventory"))]
    by_source = {str(item.get("source_rel", "")): item for item in inventory}
    for required in REQUIRED_ROLLOUT_ASSETS:
        if required not in by_source:
            errors.append(f"required rollout asset missing: {required}")

    for item in inventory:
        _validate_asset(errors, item)

    commands = set(_list_str(payload.get("cli_commands")))
    for command in CLI_COMMANDS:
        if command not in commands:
            errors.append(f"cli command missing: {command}")

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "asset_count": len(inventory),
        "required_asset_count": len(REQUIRED_ROLLOUT_ASSETS),
        "missing_required_assets": [item for item in REQUIRED_ROLLOUT_ASSETS if item not in by_source],
        "no_apply": not any(bool(payload.get(key, False)) for key in NO_APPLY_FLAGS),
        "no_runtime_execution": not bool(payload.get("would_execute_opencode", False)),
        "no_installed_runtime_claims": not any(bool(payload.get(key, False)) for key in CLAIM_FLAGS),
    }


def _build_asset(
    source_root: Path,
    target_root: Path,
    *,
    source_rel: str,
    asset_kind: str,
    phase: int,
    manifest_entries: dict[str, dict[str, Any]],
    package_glob: dict[str, Any],
    install_sync_text: str,
) -> dict[str, Any]:
    source_path = source_root / source_rel
    target_rel = _target_rel(source_rel, asset_kind)
    manifest_entry = manifest_entries.get(source_rel, {})
    covered_by_glob = asset_kind == "python_module" and bool(package_glob)
    doc_only = asset_kind == "review_evidence_doc"
    manifest_present = bool(manifest_entry)
    coverage_mode = "doc_only"
    if manifest_present:
        coverage_mode = "manifest_explicit"
    elif covered_by_glob:
        coverage_mode = "manifest_glob"
    elif not doc_only:
        coverage_mode = "missing_manifest_entry"

    coverage_entry = manifest_entry if manifest_present else package_glob if covered_by_glob else {}
    expected_smoke = _expected_smoke_required(asset_kind)
    return {
        "asset_id": source_rel.replace("/", ":"),
        "phase": phase,
        "asset_kind": asset_kind,
        "source_rel": source_rel,
        "source_path": str(source_path),
        "target_rel": target_rel,
        "target_path": str(target_root / target_rel) if target_rel else "",
        "coverage_mode": coverage_mode,
        "manifest_entry_present": manifest_present,
        "manifest_glob_present": covered_by_glob,
        "install_allowlist_present": _install_allowlist_present(source_rel, asset_kind, install_sync_text),
        "install_allowed": bool(coverage_entry.get("install_allowed", False)),
        "requires_review": bool(coverage_entry.get("requires_review", False)),
        "smoke_required_after_install": bool(coverage_entry.get("smoke_required_after_install", False)),
        "expected_smoke_required_after_install": expected_smoke,
        "source_exists": source_path.is_file(),
        "target_binding_expected": bool(target_rel),
        "notes": str(coverage_entry.get("notes", "")),
    }


def _validate_asset(errors: list[str], item: dict[str, Any]) -> None:
    source_rel = str(item.get("source_rel", ""))
    asset_kind = str(item.get("asset_kind", ""))
    if not source_rel:
        errors.append("asset source_rel missing")
        return
    if not bool(item.get("source_exists", False)):
        errors.append(f"source asset missing: {source_rel}")
    if asset_kind == "review_evidence_doc":
        return
    if not (bool(item.get("manifest_entry_present")) or bool(item.get("manifest_glob_present"))):
        errors.append(f"manifest entry missing: {source_rel}")
    if not bool(item.get("install_allowlist_present")):
        errors.append(f"install allowlist missing: {source_rel}")
    if not bool(item.get("install_allowed")):
        errors.append(f"install_allowed must be true: {source_rel}")
    if not bool(item.get("requires_review")):
        errors.append(f"requires_review must be true: {source_rel}")
    if bool(item.get("smoke_required_after_install")) != bool(item.get("expected_smoke_required_after_install")):
        errors.append(f"smoke_required_after_install mismatch: {source_rel}")
    target_rel = str(item.get("target_rel", ""))
    if target_rel.startswith((".omx/", "web-fullstack-dev/", ".git/", "tests/", "fixtures/", "docs/")):
        errors.append(f"forbidden target path: {source_rel}")
    if ".." in Path(target_rel).parts:
        errors.append(f"target path escape: {source_rel}")


def _target_rel(source_rel: str, asset_kind: str) -> str:
    if asset_kind == "bin_script":
        return source_rel
    if asset_kind == "python_module":
        prefix = "src/visible_agent_orchestrator/"
        return "lib/visible_agent_orchestrator/" + source_rel.removeprefix(prefix)
    if asset_kind in {"worker_backend_schema", "worker_backend_examples"}:
        return source_rel
    return ""


def _expected_smoke_required(asset_kind: str) -> bool:
    return asset_kind in {"bin_script", "python_module"}


def _install_allowlist_present(source_rel: str, asset_kind: str, install_sync_text: str) -> bool:
    if asset_kind == "bin_script":
        return Path(source_rel).name in install_sync_text
    if asset_kind == "python_module":
        return True
    if asset_kind in {"worker_backend_schema", "worker_backend_examples"}:
        return source_rel in install_sync_text
    return False


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _manifest_entries_by_source(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(_dict(item).get("source_path", "")): _dict(item)
        for item in _list(manifest.get("files"))
        if str(_dict(item).get("source_path", ""))
    }


def _package_glob_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    for item in _list(manifest.get("files")):
        entry = _dict(item)
        if entry.get("source_path") == "src/visible_agent_orchestrator/**/*.py":
            return entry
    return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _readme_anchors(path: Path) -> list[str]:
    text = _read_text(path)
    return [f"Phase {phase}" for phase in range(15, 21) if f"Phase {phase}" in text]


def _manifest_bindings(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_rel": item["source_rel"],
            "coverage_mode": item["coverage_mode"],
            "manifest_entry_present": item["manifest_entry_present"],
            "manifest_glob_present": item["manifest_glob_present"],
        }
        for item in inventory
    ]


def _allowlist_bindings(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_rel": item["source_rel"],
            "install_allowlist_present": item["install_allowlist_present"],
        }
        for item in inventory
        if item["asset_kind"] != "review_evidence_doc"
    ]


def _rollout_summary(inventory: list[dict[str, Any]], *, validation_ok: bool | None = None) -> dict[str, Any]:
    manifest_gaps = [
        item["source_rel"]
        for item in inventory
        if item["asset_kind"] != "review_evidence_doc"
        and not (item["manifest_entry_present"] or item["manifest_glob_present"])
    ]
    allowlist_gaps = [
        item["source_rel"]
        for item in inventory
        if item["asset_kind"] != "review_evidence_doc" and not item["install_allowlist_present"]
    ]
    source_missing = [item["source_rel"] for item in inventory if not item["source_exists"]]
    ready = not manifest_gaps and not allowlist_gaps and not source_missing
    if validation_ok is not None:
        ready = ready and validation_ok
    return {
        "ready_for_review": ready,
        "asset_count": len(inventory),
        "manifest_gap_assets": manifest_gaps,
        "allowlist_gap_assets": allowlist_gaps,
        "missing_source_assets": source_missing,
        "manifest_explicit_assets_ready": sum(1 for item in inventory if item["coverage_mode"] == "manifest_explicit"),
        "manifest_glob_assets_ready": sum(1 for item in inventory if item["coverage_mode"] == "manifest_glob"),
        "doc_alignment_ready": all(item["source_exists"] for item in inventory if item["asset_kind"] == "review_evidence_doc"),
        "runtime_proof_uninstalled": True,
        "runtime_execution_unproven": True,
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_str(value: Any) -> list[str]:
    return [str(item) for item in _list(value)]


def _list_int(value: Any) -> list[int]:
    out: list[int] = []
    for item in _list(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            return []
    return out


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
