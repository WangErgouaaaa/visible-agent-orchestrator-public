from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from visible_agent_orchestrator.worker.authorization_profiles import (
    BUSINESS_SCOPED_WRITE_KIND,
    prepare_business_scoped_write_smoke,
    validate_business_scoped_write_approval,
    validate_business_write_scope,
)


def _contract(root: Path, **updates: object) -> dict[str, object]:
    contract: dict[str, object] = {
        "task_id": "portable-business-scope",
        "business_repo": str(root),
        "allowed_files": ["src/allowed.txt"],
        "allowed_dirs": [],
        "forbidden_files": [],
        "forbidden_dirs": [],
        "max_files_changed": 2,
        "allowed_commands": ["git diff --name-only"],
        "required_validation_commands": ["python -m unittest"],
        "required_diff_checks": ["git diff --check"],
        "provenance_required": True,
        "rollback_policy": "separate approval required",
        "user_approval_id": "approval-1",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    contract.update(updates)
    return contract


def _profile() -> dict[str, object]:
    return {
        "profile_id": "business-scoped-write-v1",
        "profile_kind": BUSINESS_SCOPED_WRITE_KIND,
        "enabled": True,
        "business_repo_writes_allowed": True,
        "allowed_workers": [],
    }


def _profile_validation(payload: dict[str, object], *, profile_path: str = "") -> dict[str, object]:
    return {
        "ok": True,
        "profile_id": payload.get("profile_id", ""),
        "profile_kind": payload.get("profile_kind", ""),
        "enabled": bool(payload.get("enabled")),
        "business_repo_writes_allowed": bool(payload.get("business_repo_writes_allowed")),
        "blocked_reasons": [],
    }


def test_explicit_business_root_accepts_relative_and_absolute_aliases(tmp_path: Path) -> None:
    workspace_a = tmp_path / "workspace A"
    workspace_b = tmp_path / "workspace B"
    target = workspace_b / "src" / "allowed.txt"
    target.parent.mkdir(parents=True)
    target.write_text("ok\n", encoding="utf-8")
    workspace_a.mkdir()
    contract = _contract(workspace_b, allowed_files=[str(target)])

    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        absolute = validate_business_write_scope(_profile(), contract, changed_files=[str(target)])
        relative = validate_business_write_scope(_profile(), contract, changed_files=["src/allowed.txt"])

    assert absolute["ok"] is True
    assert relative["ok"] is True
    assert absolute["write_scope_result"]["changed_files"] == ["src/allowed.txt"]
    assert relative["write_scope_result"]["changed_files"] == ["src/allowed.txt"]


def test_explicit_business_root_rejects_other_workspace_and_symlink_escape(tmp_path: Path) -> None:
    workspace_a = tmp_path / "workspace A"
    workspace_b = tmp_path / "workspace B"
    outside = tmp_path / "outside"
    workspace_a.mkdir()
    workspace_b.mkdir()
    outside.mkdir()
    (workspace_b / "escape").symlink_to(outside, target_is_directory=True)

    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        other_workspace = validate_business_write_scope(
            _profile(),
            _contract(workspace_b, allowed_files=[str(workspace_a / "src" / "file.txt")]),
        )
        symlink_escape = validate_business_write_scope(
            _profile(),
            _contract(workspace_b, allowed_files=["escape/new.txt"]),
            changed_files=["escape/new.txt"],
        )
        parent_escape = validate_business_write_scope(
            _profile(),
            _contract(workspace_b, allowed_files=["../outside/new.txt"]),
        )

    assert other_workspace["ok"] is False
    assert symlink_escape["ok"] is False
    assert parent_escape["ok"] is False
    assert any("must resolve under business repo" in item for item in other_workspace["blocked_reasons"])
    assert any("must resolve under business repo" in item for item in symlink_escape["blocked_reasons"])


def test_alias_conflicts_and_control_paths_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    (workspace / "src").mkdir(parents=True)
    (workspace / ".omx" / "framework" / "team").mkdir(parents=True)
    (workspace / "control-alias").symlink_to(
        workspace / ".omx" / "framework" / "team", target_is_directory=True
    )
    alias_contract = _contract(
        workspace,
        allowed_files=[str(workspace / "src" / "same.txt")],
        forbidden_files=["src/same.txt"],
    )
    control_contract = _contract(
        workspace,
        allowed_files=[
            ".omx/state/team/task.json",
            ".omx/framework/team/registered-workers/registered-workers.json",
            ".omx/framework/team/opencode_visible_workers.json",
            "control-alias/opencode_visible_workers.json",
        ],
    )
    directory_alias_contract = _contract(
        workspace,
        allowed_files=[],
        allowed_dirs=[str(workspace / "src")],
        forbidden_dirs=["src"],
    )

    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        alias_result = validate_business_write_scope(_profile(), alias_contract)
        control_result = validate_business_write_scope(_profile(), control_contract)
        directory_alias_result = validate_business_write_scope(_profile(), directory_alias_contract)

    assert "allowed_files overlaps forbidden_files" in alias_result["blocked_reasons"]
    assert control_result["ok"] is False
    assert "allowed_dirs overlaps forbidden_dirs" in directory_alias_result["blocked_reasons"]
    assert any("registry path" in item for item in control_result["blocked_reasons"])
    assert any(".omx/state/team" in item for item in control_result["blocked_reasons"])


def test_changed_file_honors_canonical_forbidden_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "blocked.txt"
    target.parent.mkdir(parents=True)
    contract = _contract(
        workspace,
        allowed_dirs=["src"],
        forbidden_files=[str(target)],
    )

    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        result = validate_business_write_scope(_profile(), contract, changed_files=["src/blocked.txt"])

    assert result["ok"] is False
    assert result["write_scope_result"]["forbidden_changed_files"] == ["src/blocked.txt"]


def test_absolute_allowed_directory_accepts_relative_changed_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    contract = _contract(workspace, allowed_files=[], allowed_dirs=[str(source)])

    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        result = validate_business_write_scope(_profile(), contract, changed_files=["src/new.txt"])

    assert result["ok"] is True
    assert result["write_scope_result"]["changed_files"] == ["src/new.txt"]


def test_prep_and_approval_share_explicit_business_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace B with spaces"
    target = workspace / "src" / "allowed.txt"
    target.parent.mkdir(parents=True)
    profile_path = tmp_path / "profile.json"
    contract_path = tmp_path / "contract.json"
    profile_path.write_text(json.dumps(_profile()), encoding="utf-8")
    contract_path.write_text(json.dumps(_contract(workspace, allowed_files=[str(target)])), encoding="utf-8")

    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        prep = prepare_business_scoped_write_smoke(
            profile_path,
            contract_path,
            writer="writer-1",
            reviewer="reviewer-1",
        )

    assert prep["ok"] is True
    assert prep["business_repo"] == str(workspace)
    assert prep["writer"]["allowed_files"] == ["src/allowed.txt"]

    approval = {
        "schema_version": 1,
        "approval_kind": "business-scoped-write-approval",
        "approval_id": "approval-2",
        "approved_by": "operator",
        "profile_id": prep["profile_id"],
        "writer": "writer-1",
        "reviewer": "reviewer-1",
        "contract_path": prep["contract_path"],
        "allowed_files": [str(target)],
        "live_write_approved": True,
        "allow_registry_writes": False,
        "allow_state_team_writes": False,
        "allow_git_index_changes": False,
        "raw_tmux_allowed": False,
        "direct_tui_allowed": False,
        "direct_adapter_allowed": False,
        "rmux_allowed": False,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    result = validate_business_scoped_write_approval(approval, prep)

    assert result["ok"] is True


def test_changed_control_paths_are_forbidden_under_a_broad_allowed_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = _contract(workspace, allowed_files=[], allowed_dirs=[".omx"])
    with patch(
        "visible_agent_orchestrator.worker.authorization_profiles.validate_authorization_profile",
        side_effect=_profile_validation,
    ):
        for changed in (
            ".omx/state/team/task.json",
            ".omx/framework/team/registered-workers/registered-workers.json",
            ".omx/framework/team/opencode_visible_workers.json",
        ):
            result = validate_business_write_scope(_profile(), contract, changed_files=[changed])
            assert result["ok"] is False
            assert result["write_scope_result"]["forbidden_changed_files"] == [changed]
