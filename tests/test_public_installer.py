from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
loader = importlib.machinery.SourceFileLoader("public_installer", str(ROOT / "bin" / "omx_install_sync"))
spec = importlib.util.spec_from_loader(loader.name, loader)
installer = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = installer
loader.exec_module(installer)


def call_installer(admission: Path, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = installer.main(
            argv,
            _admission_root=admission,
        )
    return code, out.getvalue(), err.getvalue()


def invoke(admission: Path, *extra: str) -> tuple[int, str, str]:
    return call_installer(admission, ["bootstrap-companion-skills", "--task-id", "first-install", "--attempt-id", "attempt-1", *extra])


def test_first_install_bootstrap_is_scoped_consumed_and_not_replayed(tmp_path: Path) -> None:
    admission = tmp_path / "user state with spaces"
    code, out, err = invoke(admission, "--authorize-leader", "--allow-write", "--json")
    assert code == 0, err
    assert json.loads(out)["lifecycle_state"] == "OPEN"
    ledger = admission / ".omx/framework/team/operation-authorizations/first-install/attempt-1.jsonl"
    before = ledger.read_bytes()
    records = [json.loads(line) for line in before.splitlines()]
    assert [record["decision"] for record in records] == ["grant", "revoke"]
    assert records[1]["revokes_authorization_id"] == records[0]["authorization_id"]
    observed = installer.AuthorizationLedger(ledger, task_id="first-install", attempt_id="attempt-1")
    assert observed.active_authorization(records[0]["authorization_id"]) is None
    assert records[0]["capability"]["operation_kind"] == "install_sync"
    assert records[0]["capability"]["scope"] == installer.companion_bootstrap_scope()
    code, _, err = invoke(admission, "--authorize-leader", "--allow-write")
    assert code == 1 and "already exists" in err
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("extra", [[], ["--authorization-id", "another-grant"], ["--ledger-boundary", "1"]])
def test_bootstrap_requires_write_and_refuses_mixed_grant_inputs(tmp_path: Path, extra: list[str]) -> None:
    admission = tmp_path / "admission"
    flags = ["--authorize-leader", *extra]
    if extra:
        flags.append("--allow-write")
    code, _, _ = invoke(admission, *flags)
    assert code == 1
    assert not (admission / ".omx/framework/team/operation-authorizations").exists()


def test_bootstrap_rejects_a_different_control_root(tmp_path: Path) -> None:
    admission = tmp_path / "admission"
    other = tmp_path / "unrelated"
    code, _, err = invoke(admission, "--authorize-leader", "--allow-write", "--control-root", str(other))
    assert code == 1 and "match the admission root" in err
    assert not other.exists()


def test_bootstrap_checks_quiescence_before_creating_a_grant(tmp_path: Path) -> None:
    admission = tmp_path / "admission"
    with patch.object(installer, "ensure_global_quiescence", side_effect=installer.InstallSyncError("target occupied")):
        code, _, err = invoke(admission, "--authorize-leader", "--allow-write")
    assert code == 1 and "target occupied" in err
    assert not (admission / ".omx/framework/team/operation-authorizations").exists()


def test_offline_bootstrap_refuses_to_replace_installed_assets(tmp_path: Path) -> None:
    admission = tmp_path / "admission"
    assert invoke(admission, "--authorize-leader", "--allow-write")[0] == 0
    target = tmp_path / "runtime"
    existing = target / "bin/omx_install_sync"
    existing.parent.mkdir(parents=True)
    existing.write_text("operator-owned existing installer\n")
    code, _, error = call_installer(admission, ["apply", "--source", str(ROOT), "--target", str(target), "--backup-dir", str(tmp_path / "backup"), "--allow-write", "--smoke"])
    assert code == 1 and "absent install targets" in error
    assert existing.read_text() == "operator-owned existing installer\n"


@pytest.mark.parametrize("relative", [".omx/state", ".omx/framework/team/opencode_visible_workers.json", ".omx/framework/team/visible-runs"])
def test_offline_bootstrap_refuses_existing_worker_state(tmp_path: Path, relative: str) -> None:
    admission = tmp_path / "admission"
    assert invoke(admission, "--authorize-leader", "--allow-write")[0] == 0
    target = tmp_path / "runtime"
    (target / relative).mkdir(parents=True)
    code, _, error = call_installer(admission, ["apply", "--source", str(ROOT), "--target", str(target), "--backup-dir", str(tmp_path / "backup"), "--allow-write", "--smoke"])
    assert code == 1 and "unused worker runtime" in error


def test_offline_bootstrap_still_requires_explicit_smoke(tmp_path: Path) -> None:
    admission = tmp_path / "admission"
    assert invoke(admission, "--authorize-leader", "--allow-write")[0] == 0
    target = tmp_path / "runtime"
    code, _, error = call_installer(admission, ["apply", "--source", str(ROOT), "--target", str(target), "--backup-dir", str(tmp_path / "backup"), "--allow-write"])
    assert code == 1 and "post-install smoke required" in error
    assert not (target / "bin").exists()


def test_offline_bootstrap_requires_smoke_even_when_file_flags_are_disabled(tmp_path: Path) -> None:
    admission = tmp_path / "admission"
    assert invoke(admission, "--authorize-leader", "--allow-write")[0] == 0
    manifest = json.loads((ROOT / "install/visible-agent-orchestrator.install-manifest.json").read_text())
    for item in manifest["files"]:
        item["smoke_required_after_install"] = False
    alternate = tmp_path / "alternate-manifest.json"
    alternate.write_text(json.dumps(manifest))
    target = tmp_path / "runtime"
    with patch.object(installer, "apply_plan") as mutation:
        code, _, error = call_installer(admission, ["apply", "--source", str(ROOT), "--target", str(target), "--manifest", str(alternate), "--backup-dir", str(tmp_path / "backup"), "--allow-write"])
    assert code == 1 and "post-install smoke required by offline_bootstrap" in error
    mutation.assert_not_called()
    assert not (target / "bin").exists()


def test_only_the_shipped_worker_reference_is_allowlisted() -> None:
    reference = "companion-skill-sources/visible-agent-orchestrator/references/worker-contracts.md"
    installer.validate_install_record(reference, reference)
    with pytest.raises(installer.InstallSyncError, match="outside install allowlist"):
        installer.validate_install_target(reference.replace("worker-contracts.md", "other.md"))
