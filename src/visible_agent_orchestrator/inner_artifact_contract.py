"""Artifact-first inner-lane contracts for nested 4x3 / 4x7 workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


INNER_MODE_4X3 = "4x3"
INNER_MODE_4X7 = "4x7"
INNER_MODE_CHOICES = (INNER_MODE_4X3, INNER_MODE_4X7)
DEFAULT_ARTIFACT_ROOT = "inner-artifacts"
DEFAULT_DOMAIN_ARTIFACT_ROOT = "domain-artifacts"
SCHEMA_VERSION = 1


def _mode_spec(mode: str) -> dict[str, Any]:
    if mode == INNER_MODE_4X3:
        return {
            "mode": INNER_MODE_4X3,
            "description": "primary-agent fan-out with three inner lanes",
            "manifest_name": "manifest.json",
            "files": [
                {"role": "explore", "filename": "explore.md"},
                {"role": "oracle", "filename": "oracle.md"},
                {"role": "hephaestus", "filename": "hephaestus.md"},
            ],
        }
    if mode == INNER_MODE_4X7:
        return {
            "mode": INNER_MODE_4X7,
            "description": "recommended role decomposition with seven inner lanes",
            "manifest_name": "role-manifest.json",
            "files": [
                {"role": "explore", "filename": "01-explore.md"},
                {"role": "librarian", "filename": "02-librarian.md"},
                {"role": "oracle", "filename": "03-oracle.md"},
                {"role": "hephaestus", "filename": "04-hephaestus.md"},
                {"role": "metis", "filename": "05-metis.md"},
                {"role": "momus", "filename": "06-momus.md"},
                {"role": "multimodal-looker", "filename": "07-multimodal-looker.md"},
            ],
        }
    raise ValueError(f"unsupported inner artifact mode: {mode}")


def build_inner_artifact_contract(mode: str, artifact_root: str = DEFAULT_ARTIFACT_ROOT) -> dict[str, Any]:
    spec = _mode_spec(mode)
    root_name = _required_artifact_root(artifact_root, DEFAULT_ARTIFACT_ROOT)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": spec["mode"],
        "artifact_root": root_name,
        "manifest_name": spec["manifest_name"],
        "files": spec["files"],
        "description": spec["description"],
    }


def render_inner_artifact_instructions(mode: str, artifact_root: str = DEFAULT_ARTIFACT_ROOT) -> str:
    contract = build_inner_artifact_contract(mode, artifact_root)
    file_lines = [f"- `{item['filename']}` ({item['role']}) -> `$ARTIFACT_DIR/{item['filename']}`" for item in contract["files"]]
    shell_path_lines = [
        f'{item["role"].upper().replace("-", "_")}_PATH="$ARTIFACT_DIR/{item["filename"]}"'
        for item in contract["files"]
    ]
    manifest_preview = {
        "schema_version": SCHEMA_VERSION,
        "mode": contract["mode"],
        "artifact_root": contract["artifact_root"],
        "files": {item["role"]: f"{contract['artifact_root']}/{item['filename']}" for item in contract["files"]},
        "all_outputs_nonempty": True,
    }
    return "\n".join(
        [
            "",
            "## Inner Artifact Contract",
            "",
            f"Work in mode `{contract['mode']}` and write all inner outputs under `$TASK_DIR/{contract['artifact_root']}/`.",
            "The protocol block later in this prompt defines `TASK_DIR`; use that exact task directory rather than inventing another path.",
            f"Every outer visible worker that receives this prompt must create the complete `{contract['mode']}` artifact set in its own task directory.",
            "Do not split these inner roles across different outer fanout workers; other workers will not satisfy your artifact contract.",
            (
                "A worker assigned to `explore` still must create `02-librarian.md` through "
                "`07-multimodal-looker.md` with concrete content or an explicit not-applicable/blocker note."
                if contract["mode"] == INNER_MODE_4X7
                else "A worker focused on one lane still must create every required artifact with concrete content or an explicit not-applicable/blocker note."
            ),
            "Initialize the exact artifact paths before any inner-lane work:",
            "```bash",
            f'ARTIFACT_DIR="$TASK_DIR/{contract["artifact_root"]}"',
            f'MANIFEST_PATH="$ARTIFACT_DIR/{contract["manifest_name"]}"',
            *shell_path_lines,
            'mkdir -p "$ARTIFACT_DIR"',
            "```",
            f"Do not write `{contract['manifest_name']}` at `$TASK_DIR/{contract['manifest_name']}`. The only valid manifest path is `$MANIFEST_PATH`.",
            "Do not rely on session handles or background task output retrieval for the final deliverable.",
            "Task success depends on creating the required files. If any artifact file or manifest is missing, the outer task will be marked failed.",
            "Required markdown artifacts:",
            *file_lines,
            f"- `{contract['manifest_name']}` -> `$MANIFEST_PATH`",
            "",
            "Each markdown artifact must be non-empty and contain the concrete result for that lane.",
            f"You must create `{contract['manifest_name']}` as an actual file on disk inside the artifact directory. Mentioning it in `report.md` is not sufficient.",
            "The manifest must be valid JSON matching this shape:",
            "```json",
            json.dumps(manifest_preview, ensure_ascii=False, indent=2),
            "```",
            "",
            "Write the manifest file before finishing the task, and if you accidentally created a root-level manifest, move it into `$ARTIFACT_DIR` before you finish.",
            "Before you finish with success, run this exact self-check and only continue if it exits 0:",
            "```bash",
            "for required_path in \\",
            *(f'  "${item["role"].upper().replace("-", "_")}_PATH" \\' for item in contract["files"]),
            '  "$MANIFEST_PATH"',
            "do",
            '  test -s "$required_path" || { echo "missing_or_empty: $required_path" >&2; exit 1; }',
            "done",
            "```",
            "If the self-check fails, do not mark the task done. Fix the missing artifact first, or finish blocked/fail after writing `report.md` that names the missing paths.",
            "The outer `report.md` should summarize the inner artifacts rather than restating all raw work.",
        ]
    )


def render_body_with_inner_artifact_contract(body: str, mode: str, artifact_root: str = DEFAULT_ARTIFACT_ROOT) -> str:
    return f"{str(body or '').rstrip()}\n{render_inner_artifact_instructions(mode, artifact_root)}\n"


def build_domain_artifact_contract(
    domain_id: str,
    *,
    subdomains: list[dict[str, Any]] | None = None,
    artifact_root: str = DEFAULT_DOMAIN_ARTIFACT_ROOT,
) -> dict[str, Any]:
    domain = _required_domain_id(domain_id)
    root_name = _required_artifact_root(artifact_root, DEFAULT_DOMAIN_ARTIFACT_ROOT)
    domain_root = f"{root_name}/{domain}"
    normalized_subdomains = []
    for item in subdomains or []:
        if not isinstance(item, dict):
            raise ValueError("subdomain must be an object")
        subdomain_id = _required_subdomain_id(item.get("subdomain_id"))
        agent = str(item.get("agent") or "").strip()
        if not agent:
            raise ValueError(f"subdomain {subdomain_id} missing agent")
        normalized_subdomains.append(
            {
                "subdomain_id": subdomain_id,
                "agent": agent,
                "filename": f"subdomains/{subdomain_id}.json",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "domain-artifacts",
        "domain_id": domain,
        "artifact_root": domain_root,
        "domain_contract_name": "domain-contract.json",
        "domain_report_name": "domain-report.json",
        "subdomains": normalized_subdomains,
    }


def render_domain_artifact_instructions(
    domain_id: str,
    *,
    subdomains: list[dict[str, Any]] | None = None,
    artifact_root: str = DEFAULT_DOMAIN_ARTIFACT_ROOT,
) -> str:
    contract = build_domain_artifact_contract(domain_id, subdomains=subdomains, artifact_root=artifact_root)
    subdomain_lines = [
        f"- `{item['filename']}` ({item['agent']}) -> `$DOMAIN_ARTIFACT_DIR/{item['filename']}`"
        for item in contract["subdomains"]
    ]
    return "\n".join(
        [
            "",
            "## Domain Artifact Contract",
            "",
            f"Own evidence domain `{contract['domain_id']}` and write a compact domain bundle under `$TASK_DIR/{contract['artifact_root']}/`.",
            'DOMAIN_ARTIFACT_DIR="$TASK_DIR/' + contract["artifact_root"] + '"',
            'DOMAIN_CONTRACT_PATH="$DOMAIN_ARTIFACT_DIR/domain-contract.json"',
            'DOMAIN_REPORT_PATH="$DOMAIN_ARTIFACT_DIR/domain-report.json"',
            "mkdir -p \"$DOMAIN_ARTIFACT_DIR/subdomains\"",
            "",
            "Required JSON artifacts:",
            "- `domain-contract.json` -> `$DOMAIN_CONTRACT_PATH`",
            *subdomain_lines,
            "- `domain-report.json` -> `$DOMAIN_REPORT_PATH`",
            "",
            "Each subdomain JSON must be compact, evidence-backed, and scoped to its allowed sources.",
            "The domain report must summarize merged subdomains, conflicts, uncertainties, and recommended merge action.",
            "The outer report should summarize `domain-report.json` instead of restating raw subagent prose.",
        ]
    )


def render_body_with_domain_artifact_contract(
    body: str,
    *,
    domain_id: str,
    subdomains: list[dict[str, Any]] | None = None,
    artifact_root: str = DEFAULT_DOMAIN_ARTIFACT_ROOT,
) -> str:
    return f"{str(body or '').rstrip()}\n{render_domain_artifact_instructions(domain_id, subdomains=subdomains, artifact_root=artifact_root)}\n"


def normalize_inner_artifacts(
    root: Path,
    run_id: str,
    task_id: str,
    mode: str,
    artifact_root: str = DEFAULT_ARTIFACT_ROOT,
) -> dict[str, Any]:
    contract = build_inner_artifact_contract(mode, artifact_root)
    task_dir = Path(root).resolve() / ".omx" / "framework" / "team" / "visible-runs" / run_id / "tasks" / task_id
    artifacts_dir = task_dir / contract["artifact_root"]
    manifest_path = artifacts_dir / contract["manifest_name"]
    manifest_fallback_path = task_dir / contract["manifest_name"]
    if manifest_path.is_file() or not manifest_fallback_path.is_file():
        return {
            "normalized": False,
            "warning": "",
            "manifest_path": str(manifest_path if manifest_path.is_file() else manifest_fallback_path),
        }
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest_fallback_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "normalized": True,
        "warning": f"manifest_root_normalized: {contract['manifest_name']}",
        "manifest_path": str(manifest_path),
    }


def validate_inner_artifacts(
    root: Path,
    run_id: str,
    task_id: str,
    mode: str,
    artifact_root: str = DEFAULT_ARTIFACT_ROOT,
) -> dict[str, Any]:
    contract = build_inner_artifact_contract(mode, artifact_root)
    task_dir = Path(root).resolve() / ".omx" / "framework" / "team" / "visible-runs" / run_id / "tasks" / task_id
    artifacts_dir = task_dir / contract["artifact_root"]
    files: list[dict[str, Any]] = []
    missing: list[str] = []
    empty: list[str] = []
    for item in contract["files"]:
        path = artifacts_dir / item["filename"]
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        nonempty = bool(exists and size > 0)
        files.append(
            {
                "role": item["role"],
                "filename": item["filename"],
                "path": str(path),
                "exists": exists,
                "nonempty": nonempty,
                "bytes": size,
            }
        )
        if not exists:
            missing.append(item["filename"])
        elif not nonempty:
            empty.append(item["filename"])

    manifest_path = artifacts_dir / contract["manifest_name"]
    manifest_fallback_path = task_dir / contract["manifest_name"]
    manifest_data: dict[str, Any] = {}
    manifest_exists = manifest_path.is_file()
    manifest_valid = False
    parse_error = ""
    used_fallback_manifest = False
    actual_manifest_path = manifest_path
    if not manifest_exists and manifest_fallback_path.is_file():
        manifest_exists = True
        used_fallback_manifest = True
        actual_manifest_path = manifest_fallback_path
    if manifest_exists:
        try:
            loaded = json.loads(actual_manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest_data = loaded
                manifest_valid = (
                    int(loaded.get("schema_version", 0) or 0) == SCHEMA_VERSION
                    and str(loaded.get("mode") or "") == contract["mode"]
                )
        except Exception as exc:
            parse_error = str(exc)

    errors: list[str] = []
    if not artifacts_dir.is_dir():
        errors.append(f"artifact_root_missing: {artifacts_dir}")
    if missing:
        errors.append("missing_artifacts: " + ", ".join(missing))
    if empty:
        errors.append("empty_artifacts: " + ", ".join(empty))
    if not manifest_exists:
        errors.append(f"manifest_missing: {contract['manifest_name']}")
    elif not manifest_valid:
        errors.append(f"manifest_invalid: {contract['manifest_name']}")
    if parse_error:
        errors.append(f"manifest_parse_error: {parse_error}")
    warnings: list[str] = []
    if used_fallback_manifest:
        warnings.append(f"manifest_root_fallback_used: {contract['manifest_name']}")

    if errors:
        state = "artifact_missing" if missing or not manifest_exists else "partial_role_output"
    else:
        state = "completed"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": contract["mode"],
        "artifact_root": contract["artifact_root"],
        "task_dir": str(task_dir),
        "artifacts_dir": str(artifacts_dir),
        "manifest_name": contract["manifest_name"],
        "manifest_path": str(actual_manifest_path),
        "manifest_exists": manifest_exists,
        "manifest_valid": manifest_valid,
        "used_fallback_manifest": used_fallback_manifest,
        "manifest": manifest_data,
        "files": files,
        "missing": missing,
        "empty": empty,
        "ok": not errors,
        "state": state,
        "errors": errors,
        "warnings": warnings,
    }


def validate_domain_artifacts(
    root: Path,
    run_id: str,
    task_id: str,
    domain_id: str,
    artifact_root: str = DEFAULT_DOMAIN_ARTIFACT_ROOT,
) -> dict[str, Any]:
    domain = _required_domain_id(domain_id)
    task_dir = Path(root).resolve() / ".omx" / "framework" / "team" / "visible-runs" / run_id / "tasks" / task_id
    artifacts_dir = task_dir / _required_artifact_root(artifact_root, DEFAULT_DOMAIN_ARTIFACT_ROOT) / domain
    contract_path = artifacts_dir / "domain-contract.json"
    report_path = artifacts_dir / "domain-report.json"

    missing: list[str] = []
    errors: list[str] = []
    contract_data, contract_error = _read_json_object(contract_path)
    if not contract_path.is_file():
        missing.append("domain-contract.json")
    elif contract_error:
        errors.append(f"domain_contract_invalid: {contract_error}")
    elif int(contract_data.get("schema_version", 0) or 0) != SCHEMA_VERSION or str(contract_data.get("domain_id") or "") != domain:
        errors.append("domain_contract_invalid: domain-contract.json")

    subdomains: list[dict[str, Any]] = []
    raw_subdomains = contract_data.get("subdomains") if isinstance(contract_data.get("subdomains"), list) else []
    for item in raw_subdomains:
        if isinstance(item, dict):
            subdomains.append(item)
    subdomain_results: list[dict[str, Any]] = []
    for item in subdomains:
        try:
            subdomain_id = _required_subdomain_id(item.get("subdomain_id"))
        except ValueError as exc:
            errors.append(f"subdomain_contract_invalid: {exc}")
            continue
        path = artifacts_dir / "subdomains" / f"{subdomain_id}.json"
        data, error = _read_json_object(path)
        exists = path.is_file()
        if not exists:
            missing.append(f"subdomains/{subdomain_id}.json")
        elif error:
            errors.append(f"subdomain_invalid: {subdomain_id}: {error}")
        subdomain_results.append(
            {
                "subdomain_id": subdomain_id,
                "path": str(path),
                "exists": exists,
                "valid": bool(exists and not error),
                "artifact": data,
            }
        )

    domain_report, report_error = _read_json_object(report_path)
    if not report_path.is_file():
        missing.append("domain-report.json")
    elif report_error:
        errors.append(f"domain_report_invalid: {report_error}")
    elif str(domain_report.get("domain_id") or "") != domain:
        errors.append("domain_report_invalid: domain_id mismatch")

    ok = not missing and not errors
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "domain-artifacts",
        "domain_id": domain,
        "artifact_root": str(artifacts_dir.relative_to(task_dir)) if artifacts_dir.is_absolute() else str(artifacts_dir),
        "task_dir": str(task_dir),
        "artifacts_dir": str(artifacts_dir),
        "domain_contract_path": str(contract_path),
        "domain_report_path": str(report_path),
        "subdomains": subdomain_results,
        "domain_contract": contract_data,
        "domain_report": domain_report,
        "missing": missing,
        "errors": errors,
        "ok": ok,
        "state": "completed" if ok else "domain_artifact_missing",
    }


def _required_domain_id(value: str) -> str:
    domain = str(value or "").strip().replace("\\", "/").strip("/")
    if not domain:
        raise ValueError("domain_id is required")
    if ".." in [part for part in domain.split("/") if part]:
        raise ValueError("domain_id must not contain path traversal")
    return domain


def _required_artifact_root(value: str, default: str) -> str:
    artifact_root = str(value or default).strip().replace("\\", "/").strip("/")
    if not artifact_root:
        raise ValueError("artifact_root is required")
    if ".." in [part for part in artifact_root.split("/") if part]:
        raise ValueError("artifact_root must not contain path traversal")
    return artifact_root


def _required_subdomain_id(value: Any) -> str:
    raw = str(value or "").strip()
    subdomain_id = raw.replace("\\", "/")
    if not subdomain_id:
        raise ValueError("subdomain missing subdomain_id")
    if "/" in subdomain_id or subdomain_id in {".", ".."}:
        raise ValueError("subdomain_id must be a single safe path component")
    return subdomain_id


def _read_json_object(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, "expected object JSON"
    return data, ""
