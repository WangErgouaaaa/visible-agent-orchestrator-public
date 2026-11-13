from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REVIEW_KIND = "opencode-runtime-proof-install-sync-review"
REVIEW_STATE = "review_only"
REVIEW_MODE = "dry-run-plan-no-apply"
ROLLOUT_REVIEW_KIND = "opencode-runtime-proof-rollout-review"
PHASE_RANGE = [15, 16, 17, 18, 19, 20, 23, 26, 30]

CLI_HOST = "bin/omx_opencode_raw_capture_readiness"
SOURCE_MODULES = [
    "src/visible_agent_orchestrator/opencode_runtime_proof_authorization.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_runner_plan.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_execution_wrapper.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_postcheck_contract.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_executor_contract.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_rollout_review.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_executor_writer.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_evidence_postcheck.py",
    "src/visible_agent_orchestrator/opencode_runtime_proof_one_shot_executor.py",
]
SCHEMA_ASSETS = [
    "worker-backends/opencode-runtime-proof-authorization-package.schema.json",
    "worker-backends/opencode-runtime-proof-runner-plan.schema.json",
    "worker-backends/opencode-runtime-proof-execution-wrapper.schema.json",
    "worker-backends/opencode-runtime-proof-postcheck-contract.schema.json",
    "worker-backends/opencode-runtime-proof-executor-contract.schema.json",
    "worker-backends/opencode-runtime-proof-rollout-review.schema.json",
    "worker-backends/opencode-runtime-proof-executor-writer.schema.json",
    "worker-backends/opencode-runtime-proof-evidence-postcheck.schema.json",
    "worker-backends/opencode-runtime-proof-one-shot-result.schema.json",
]
EXAMPLES_ASSETS = [
    "worker-backends/opencode-runtime-proof-authorization-package.examples.md",
    "worker-backends/opencode-runtime-proof-runner-plan.examples.md",
    "worker-backends/opencode-runtime-proof-execution-wrapper.examples.md",
    "worker-backends/opencode-runtime-proof-postcheck-contract.examples.md",
    "worker-backends/opencode-runtime-proof-executor-contract.examples.md",
    "worker-backends/opencode-runtime-proof-rollout-review.examples.md",
    "worker-backends/opencode-runtime-proof-executor-writer.examples.md",
    "worker-backends/opencode-runtime-proof-evidence-postcheck.examples.md",
    "worker-backends/opencode-runtime-proof-one-shot-result.examples.md",
]
REQUIRED_INSTALL_SYNC_REVIEW_ASSETS = [CLI_HOST, *SOURCE_MODULES, *SCHEMA_ASSETS, *EXAMPLES_ASSETS]

NO_APPLY_FLAGS = [
    "would_run_install_sync_apply",
    "would_write_data_sync",
    "would_copy_files",
    "would_chmod",
    "would_run_live_smoke",
    "would_execute_opencode",
    "would_touch_tmux",
    "would_call_tui",
    "would_write_task_local",
    "would_write_registry",
    "would_write_state_team",
    "claims_installed_parity",
    "claims_runtime_execution_proven",
]
SAFE_ACTIONS = {"would_create", "would_update", "would_chmod", "up_to_date"}
FORBIDDEN_TARGET_PREFIXES = (".omx/", "web-fullstack-dev/", ".git/", "tests/", "fixtures/", "docs/")
PROVENANCE_GATED_RUNTIME_ASSETS = {
    "bin/omx_orchestrator_loop",
    "src/visible_agent_orchestrator/orchestrator_loop.py",
}
PROVENANCE_ARTIFACT_REL = "install/runtime-source-provenance.json"


def build_runtime_proof_install_sync_review(
    *,
    review_id: str,
    source_root: str | Path,
    target_root: str | Path,
    rollout_review: dict[str, Any],
    install_plan: dict[str, Any],
) -> dict[str, Any]:
    source = Path(source_root).resolve()
    target = Path(target_root)
    plan_items = _plan_items_by_source(install_plan)

    inventory = [
        _build_asset(source, target, source_rel=CLI_HOST, phase=15, asset_kind="bin_script", plan_items=plan_items)
    ]
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=source_rel,
            phase=phase,
            asset_kind="python_module",
            plan_items=plan_items,
        )
        for phase, source_rel in zip(PHASE_RANGE, SOURCE_MODULES, strict=True)
    )
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=source_rel,
            phase=phase,
            asset_kind="worker_backend_schema",
            plan_items=plan_items,
        )
        for phase, source_rel in zip(PHASE_RANGE, SCHEMA_ASSETS, strict=True)
    )
    inventory.extend(
        _build_asset(
            source,
            target,
            source_rel=source_rel,
            phase=phase,
            asset_kind="worker_backend_examples",
            plan_items=plan_items,
        )
        for phase, source_rel in zip(PHASE_RANGE, EXAMPLES_ASSETS, strict=True)
    )

    install_plan_summary = _install_plan_summary(install_plan)
    review = {
        "schema_version": SCHEMA_VERSION,
        "review_kind": REVIEW_KIND,
        "review_id": str(review_id),
        "review_state": REVIEW_STATE,
        "review_mode": REVIEW_MODE,
        "source_root": str(source),
        "target_root": str(target),
        "phase_range": list(PHASE_RANGE),
        "source_rollout_review_kind": str(rollout_review.get("review_kind", "")),
        "source_rollout_review_validation": _dict(rollout_review.get("validation")),
        "source_rollout_review_sha256": _stable_sha256(rollout_review),
        "install_plan_sha256": _stable_sha256(install_plan),
        "install_plan_summary": install_plan_summary,
        "asset_inventory": inventory,
        "install_plan_bindings": _install_plan_bindings(inventory),
        "parity_review": _parity_review(inventory),
        "installed_runtime_provenance_review": _installed_runtime_provenance_review(
            install_plan,
            source_root=source,
        ),
        "install_sync_review_summary": _review_summary(inventory, install_plan_summary),
        "reviewer_ack_kind": "none",
        "reviewer_ack_scope": "review-only-no-apply",
        "reviewer_ack_expires_at": "",
        "stale_installed_state_claim": False,
        "unsafe_apply_wording_detected": False,
        "unsafe_diff_wording_detected": False,
        "unsafe_write_claims_detected": False,
        "no_apply_flags": {key: False for key in NO_APPLY_FLAGS},
        "safe_next_actions": [
            "review this install/sync bundle as source-only dry-run evidence",
            "resolve any dry-run plan gaps before requesting install/sync apply",
            "request separate explicit user authorization before any future install/sync apply or runtime proof",
        ],
        "forbidden_next_actions": [
            "do not run install/sync apply from this review bundle",
            "do not write the selected runtime root from this review bundle",
            "do not copy files, chmod targets, or run live smoke",
            "do not execute OpenCode, touch tmux, call /tui, or load plugins",
            "do not write task-local protocol markers, registries, or .omx/state/team",
            "do not claim installed parity or runtime proof from dry-run review evidence",
        ],
        "advisory": (
            "Source-only no-apply install/sync review bundle; it consumes dry-run install plan JSON "
            "and inventories Phase 15-20 plus Phase 23 and Phase 26 runtime-proof CLI/module/schema/docs assets. "
            "Future install/sync "
            "apply still needs separate explicit authorization."
        ),
    }
    for key in NO_APPLY_FLAGS:
        review[key] = False
    review["validation"] = validate_runtime_proof_install_sync_review(review)
    review["blocked_reasons"] = list(review["validation"]["errors"])
    review["warnings"] = list(review["validation"]["warnings"])
    review["install_sync_review_summary"] = _review_summary(
        inventory,
        install_plan_summary,
        validation_ok=review["validation"]["ok"],
    )
    return review


def validate_runtime_proof_install_sync_review(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings = [
        "Runtime-proof install/sync review validation is source-only/no-apply",
        "Validation does not run install/sync apply, copy files, chmod targets, run live smoke, or execute OpenCode",
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
        errors.append("review_mode must be dry-run-plan-no-apply")
    if _list_int(payload.get("phase_range")) != PHASE_RANGE:
        errors.append(f"phase_range must be {PHASE_RANGE}")
    if payload.get("source_rollout_review_kind") != ROLLOUT_REVIEW_KIND:
        errors.append(f"source_rollout_review_kind must be {ROLLOUT_REVIEW_KIND}")
    rollout_validation = _dict(payload.get("source_rollout_review_validation"))
    if rollout_validation.get("ok") is not True:
        errors.append("source rollout review validation must be ok")

    ack_kind = str(payload.get("reviewer_ack_kind", "none"))
    ack_scope = str(payload.get("reviewer_ack_scope", ""))
    if ack_kind not in {"none", "review-only"}:
        errors.append("reviewer_ack_kind must be none or review-only")
    if ack_kind == "review-only" and ack_scope != "review-only-no-apply":
        errors.append("reviewer_ack_scope must be review-only-no-apply")
    for key in [
        "stale_installed_state_claim",
        "unsafe_apply_wording_detected",
        "unsafe_diff_wording_detected",
        "unsafe_write_claims_detected",
    ]:
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")

    no_apply_flags = _dict(payload.get("no_apply_flags"))
    for key in NO_APPLY_FLAGS:
        if bool(payload.get(key, False)):
            errors.append(f"{key} must be false")
        if bool(no_apply_flags.get(key, False)):
            errors.append(f"no_apply_flags.{key} must be false")

    summary = _dict(payload.get("install_plan_summary"))
    if summary.get("dry_run") is not True:
        errors.append("install plan must be dry_run")
    if summary.get("dry_run_only") is not True:
        errors.append("install plan must be dry_run_only")
    for key in ["writes_target", "copies_files", "chmod", "live_smoke"]:
        if bool(summary.get(key, False)):
            errors.append(f"install plan {key} must be false")

    inventory = [_dict(item) for item in _list(payload.get("asset_inventory"))]
    by_source = {str(item.get("source_rel", "")): item for item in inventory}
    for required in REQUIRED_INSTALL_SYNC_REVIEW_ASSETS:
        if required not in by_source:
            errors.append(f"required install-sync review asset missing: {required}")

    for item in inventory:
        _validate_asset(errors, item)
    provenance_review = _dict(payload.get("installed_runtime_provenance_review"))
    for item in _list(provenance_review.get("gated_assets")):
        _validate_installed_runtime_provenance(errors, _dict(item))

    return {
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": warnings,
        "asset_count": len(inventory),
        "required_asset_count": len(REQUIRED_INSTALL_SYNC_REVIEW_ASSETS),
        "missing_required_assets": [item for item in REQUIRED_INSTALL_SYNC_REVIEW_ASSETS if item not in by_source],
        "dry_run_plan_verified": not any(
            [
                summary.get("dry_run") is not True,
                summary.get("dry_run_only") is not True,
                bool(summary.get("writes_target", False)),
                bool(summary.get("copies_files", False)),
                bool(summary.get("chmod", False)),
                bool(summary.get("live_smoke", False)),
            ]
        ),
        "no_apply": not any(bool(payload.get(key, False)) for key in NO_APPLY_FLAGS),
        "no_runtime_execution": not bool(payload.get("would_execute_opencode", False)),
        "no_installed_parity_claims": not bool(payload.get("claims_installed_parity", False)),
        "installed_runtime_provenance_ok": not any(
            str(error).startswith("installed runtime provenance missing:")
            for error in errors
        ),
    }


def _build_asset(
    source_root: Path,
    target_root: Path,
    *,
    source_rel: str,
    phase: int,
    asset_kind: str,
    plan_items: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    item = plan_items.get(source_rel, {})
    target_rel = str(item.get("target_rel", "")) or _expected_target_rel(source_rel, asset_kind)
    source_path = str(item.get("source_path", "")) or str(source_root / source_rel)
    target_path = str(item.get("target_path", "")) or str(target_root / target_rel)
    return {
        "asset_id": source_rel.replace("/", ":"),
        "phase": phase,
        "asset_kind": asset_kind,
        "source_rel": source_rel,
        "target_rel": target_rel,
        "source_path": source_path,
        "target_path": target_path,
        "install_plan_item_present": bool(item),
        "file_kind": str(item.get("file_kind", "")),
        "action": str(item.get("action", "")),
        "source_exists": bool(item.get("source_exists", False)),
        "target_exists": bool(item.get("target_exists", False)),
        "missing_target": bool(item.get("missing_target", False)),
        "install_allowed": bool(item.get("install_allowed", False)),
        "requires_review": bool(item.get("requires_review", False)),
        "smoke_required_after_install": bool(item.get("smoke_required_after_install", False)),
        "expected_smoke_required_after_install": _expected_smoke_required(asset_kind),
        "source_sha256": str(item.get("source_sha256", "")),
        "target_sha256": str(item.get("target_sha256", "")),
        "notes": str(item.get("notes", "")),
    }


def _validate_asset(errors: list[str], item: dict[str, Any]) -> None:
    source_rel = str(item.get("source_rel", ""))
    asset_kind = str(item.get("asset_kind", ""))
    if not source_rel:
        errors.append("asset source_rel missing")
        return
    if source_rel not in REQUIRED_INSTALL_SYNC_REVIEW_ASSETS:
        errors.append(f"unexpected install-sync review asset: {source_rel}")
    if not bool(item.get("install_plan_item_present", False)):
        errors.append(f"install plan item missing: {source_rel}")
    if not bool(item.get("source_exists", False)):
        errors.append(f"source asset missing: {source_rel}")
    if not bool(item.get("install_allowed", False)):
        errors.append(f"install_allowed must be true: {source_rel}")
    if not bool(item.get("requires_review", False)):
        errors.append(f"requires_review must be true: {source_rel}")
    if str(item.get("action", "")) not in SAFE_ACTIONS:
        errors.append(f"install plan action unsafe: {source_rel}")
    if bool(item.get("smoke_required_after_install")) != bool(item.get("expected_smoke_required_after_install")):
        errors.append(f"smoke_required_after_install mismatch: {source_rel}")
    if not _valid_sha(str(item.get("source_sha256", ""))):
        errors.append(f"source_sha256 missing or invalid: {source_rel}")
    expected_file_kind = _expected_file_kind(asset_kind)
    if str(item.get("file_kind", "")) != expected_file_kind:
        errors.append(f"file_kind must be {expected_file_kind}: {source_rel}")
    target_rel = str(item.get("target_rel", ""))
    expected_target_rel = _expected_target_rel(source_rel, asset_kind)
    if target_rel != expected_target_rel:
        errors.append(f"target_rel mismatch: {source_rel}")
    if target_rel.startswith(FORBIDDEN_TARGET_PREFIXES):
        errors.append(f"forbidden target path: {source_rel}")
    if ".." in Path(target_rel).parts:
        errors.append(f"target path escape: {source_rel}")


def _plan_items_by_source(install_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = _list(install_plan.get("files"))
    if not items:
        items = _list(install_plan.get("items"))
    return {
        str(_dict(item).get("source_rel", "")): _dict(item)
        for item in items
        if str(_dict(item).get("source_rel", ""))
    }


def _install_plan_summary(install_plan: dict[str, Any]) -> dict[str, Any]:
    files = [_dict(item) for item in _list(install_plan.get("files"))]
    if not files:
        files = [_dict(item) for item in _list(install_plan.get("items"))]
    action_counts: dict[str, int] = {}
    for item in files:
        action = str(item.get("action", ""))
        action_counts[action] = action_counts.get(action, 0) + 1
    return {
        "dry_run": bool(install_plan.get("dry_run", False)),
        "dry_run_only": bool(install_plan.get("dry_run_only", False)),
        "writes_target": bool(install_plan.get("writes_target", False)),
        "copies_files": bool(install_plan.get("copies_files", False)),
        "chmod": bool(install_plan.get("chmod", False)),
        "live_smoke": bool(install_plan.get("live_smoke", False)),
        "source_root": str(install_plan.get("source_root", "")),
        "target_root": str(install_plan.get("target_root", "")),
        "manifest_path": str(install_plan.get("manifest_path", "")),
        "file_count": len(files),
        "action_counts": action_counts,
    }


def _review_summary(
    inventory: list[dict[str, Any]],
    install_plan_summary: dict[str, Any],
    *,
    validation_ok: bool | None = None,
) -> dict[str, Any]:
    missing_plan = [item["source_rel"] for item in inventory if not item["install_plan_item_present"]]
    missing_source = [item["source_rel"] for item in inventory if not item["source_exists"]]
    unsafe_actions = [item["source_rel"] for item in inventory if item["action"] not in SAFE_ACTIONS]
    review_ready = not missing_plan and not missing_source and not unsafe_actions
    review_ready = review_ready and bool(install_plan_summary.get("dry_run", False))
    review_ready = review_ready and bool(install_plan_summary.get("dry_run_only", False))
    review_ready = review_ready and not any(
        bool(install_plan_summary.get(key, False)) for key in ["writes_target", "copies_files", "chmod", "live_smoke"]
    )
    if validation_ok is not None:
        review_ready = review_ready and validation_ok
    return {
        "ready_for_install_review": review_ready,
        "runtime_proof_asset_count": len(inventory),
        "dry_run_plan_verified": bool(install_plan_summary.get("dry_run", False))
        and bool(install_plan_summary.get("dry_run_only", False))
        and not any(
            bool(install_plan_summary.get(key, False))
            for key in ["writes_target", "copies_files", "chmod", "live_smoke"]
        ),
        "missing_install_plan_assets": missing_plan,
        "missing_source_assets": missing_source,
        "unsafe_action_assets": unsafe_actions,
        "install_apply_unexecuted": True,
        "installed_parity_unclaimed": True,
        "runtime_execution_unproven": True,
    }


def _install_plan_bindings(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_rel": item["source_rel"],
            "target_rel": item["target_rel"],
            "action": item["action"],
            "source_sha256": item["source_sha256"],
            "target_sha256": item["target_sha256"],
            "install_plan_item_present": item["install_plan_item_present"],
        }
        for item in inventory
    ]


def _parity_review(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "installed_parity_claimed": False,
        "installed_parity_unclaimed": True,
        "target_states_observed_from_dry_run_only": True,
        "up_to_date_assets": [item["source_rel"] for item in inventory if item["action"] == "up_to_date"],
        "would_create_assets": [item["source_rel"] for item in inventory if item["action"] == "would_create"],
        "would_update_assets": [item["source_rel"] for item in inventory if item["action"] == "would_update"],
        "would_chmod_assets": [item["source_rel"] for item in inventory if item["action"] == "would_chmod"],
    }


def _installed_runtime_provenance_review(
    install_plan: dict[str, Any],
    *,
    source_root: Path | None = None,
) -> dict[str, Any]:
    files = [_dict(item) for item in _list(install_plan.get("files"))]
    if not files:
        files = [_dict(item) for item in _list(install_plan.get("items"))]
    recovered_provenance = _load_recovered_runtime_provenance(source_root)
    gated_assets: list[dict[str, Any]] = []
    for item in files:
        source_rel = str(item.get("source_rel") or "")
        target_rel = str(item.get("target_rel") or "")
        if source_rel not in PROVENANCE_GATED_RUNTIME_ASSETS:
            continue
        action = str(item.get("action") or "")
        source_sha = str(item.get("source_sha256") or "")
        target_sha = str(item.get("target_sha256") or "")
        differs = action in {"would_update", "would_chmod"} or bool(
            source_sha and target_sha and source_sha != target_sha
        )
        reviewed_source_ref = str(item.get("reviewed_source_ref") or "").strip()
        release_artifact_ref = str(item.get("release_artifact_ref") or "").strip()
        recovered_item = _dict(recovered_provenance.get(source_rel))
        if (
            differs
            and not reviewed_source_ref
            and not release_artifact_ref
            and str(recovered_item.get("reviewed_source_sha256") or "") == source_sha
        ):
            release_artifact_ref = PROVENANCE_ARTIFACT_REL
        gated_assets.append(
            {
                "source_rel": source_rel,
                "target_rel": target_rel,
                "action": action,
                "source_sha256": source_sha,
                "target_sha256": target_sha,
                "source_matches_installed": bool(source_sha and target_sha and source_sha == target_sha),
                "requires_reviewed_source_or_release_artifact": differs,
                "provenance_status": "provided" if differs and (reviewed_source_ref or release_artifact_ref) else (
                    "required" if differs else "not_required"
                ),
                "reviewed_source_ref": reviewed_source_ref,
                "release_artifact_ref": release_artifact_ref,
            }
        )
    missing = [
        item["source_rel"]
        for item in gated_assets
        if bool(item.get("requires_reviewed_source_or_release_artifact"))
        and not (item.get("reviewed_source_ref") or item.get("release_artifact_ref"))
    ]
    return {
        "gated_assets": gated_assets,
        "missing_provenance_assets": missing,
        "review_requirement": "reviewed_source_ref_or_release_artifact_ref_required_when_installed_target_differs",
    }


def _load_recovered_runtime_provenance(source_root: Path | None) -> dict[str, Any]:
    if source_root is None:
        return {}
    path = source_root / PROVENANCE_ARTIFACT_REL
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if payload.get("provenance_kind") != "public_source_release":
        return {}
    files = payload.get("files")
    return files if isinstance(files, dict) else {}


def _validate_installed_runtime_provenance(errors: list[str], item: dict[str, Any]) -> None:
    source_rel = str(item.get("source_rel") or "")
    if source_rel not in PROVENANCE_GATED_RUNTIME_ASSETS:
        return
    source_sha = str(item.get("source_sha256") or "")
    target_sha = str(item.get("target_sha256") or "")
    action = str(item.get("action") or "")
    differs = action in {"would_update", "would_chmod"} or bool(source_sha and target_sha and source_sha != target_sha)
    explicitly_required = bool(item.get("requires_reviewed_source_or_release_artifact"))
    has_evidence = bool(str(item.get("reviewed_source_ref") or "").strip()) or bool(
        str(item.get("release_artifact_ref") or "").strip()
    )
    if (differs or explicitly_required) and not has_evidence:
        errors.append(f"installed runtime provenance missing: {source_rel}")


def _expected_file_kind(asset_kind: str) -> str:
    if asset_kind == "bin_script":
        return "bin_script"
    if asset_kind == "python_module":
        return "python_package"
    return "worker_backend_asset"


def _expected_smoke_required(asset_kind: str) -> bool:
    return asset_kind in {"bin_script", "python_module"}


def _expected_target_rel(source_rel: str, asset_kind: str) -> str:
    if asset_kind == "bin_script":
        return source_rel
    if asset_kind == "python_module":
        return "lib/visible_agent_orchestrator/" + source_rel.removeprefix("src/visible_agent_orchestrator/")
    return source_rel


def _stable_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _valid_sha(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
