from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

from visible_agent_orchestrator.worker.authorization_profiles import (
    _common_profile_blockers,
    authorization_profile_surface_summary,
)
from visible_agent_orchestrator.worker.mixed_noop_live_runner import (
    MixedNoopLiveRequest,
    build_mixed_noop_live_plan,
)


ROOT = Path(__file__).resolve().parents[1]


def load_bin(name: str):
    loader = importlib.machinery.SourceFileLoader(f"public_selection_{name}", str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_public_worker_entrypoints_require_operator_selected_workers(tmp_path: Path, monkeypatch) -> None:
    selected_root = tmp_path / "selected runtime with spaces"
    monkeypatch.setenv("VAO_ROOT", str(selected_root))
    backend = load_bin("omx_worker_backend")
    parser = backend.build_parser()
    assert parser.parse_args(["list"]).root == selected_root
    with pytest.raises(SystemExit):
        parser.parse_args(["mixed-pressure-live-plan"])
    smoke = load_bin("omx_multiworker_smoke")
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(["plan", "--root", str(selected_root)])
    args = smoke.build_parser().parse_args(["plan", "--root", str(selected_root), "--workers", "writer-blue,reviewer-green"])
    assert smoke.parse_workers(args.workers) == ["writer-blue", "reviewer-green"]
    assert authorization_profile_surface_summary(selected_root)["configured"] is False


def test_authorization_profile_uses_selected_ids_and_rejects_unsafe_or_missing_ids() -> None:
    profile = {
        "profile_id": "selected-profile", "profile_version": 1, "profile_kind": "selected-kind",
        "allowed_workers": ["writer-blue", "reviewer-green"], "required_workers": ["writer-blue"],
        "allowed_worker_backends": ["opencode_tui"],
        "retry_policy": {"kind": "none", "max_retries": 0}, "max_live_attempts": 1,
    }
    for key in ("required_preflight_checks", "required_post_checks", "required_result_validation", "required_terminal_truth", "required_outputs", "stop_conditions", "final_report_requirements"):
        profile[key] = ["task-defined requirement"]
    assert _common_profile_blockers(profile, "selected-profile", "selected-kind") == []
    for invalid in ([], [""], ["../outside"], ["writer-blue", "writer-blue"]):
        assert _common_profile_blockers({**profile, "allowed_workers": invalid}, "selected-profile", "selected-kind")
    assert _common_profile_blockers({**profile, "required_workers": ["unselected-worker"]}, "selected-profile", "selected-kind")


def test_mixed_noop_without_selection_is_blocked_and_does_not_create_runtime(tmp_path: Path) -> None:
    root = tmp_path / "unused runtime"
    request = MixedNoopLiveRequest(
        root=str(root), registered_worker_registry_path=str(tmp_path / "registered.json"),
        opencode_visible_registry_path=str(tmp_path / "visible.json"),
    )
    result = build_mixed_noop_live_plan(request)
    assert request.include_workers == []
    assert result["ok"] is False and result["would_execute_live"] is False
    assert any("exactly one OpenCode worker and one Claude worker" in reason for reason in result["blocked_reasons"])
    assert not root.exists()


def test_recovery_and_rebind_use_selected_identity_without_host_fallback(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "bin"))
    recovery = load_bin("omx_visible_recovery")
    with pytest.raises(recovery.RecoveryError, match="identity and listener port are required"):
        recovery.build_plan({"status_flags": {"binding_stale": True}})
    plan = recovery.build_plan({"default_worker": {"worker_id": "worker-blue", "port": 12345}, "status_flags": {"binding_stale": True}})
    assert "--worker worker-blue --port 12345" in plan["actions"][0]["command"]
    rebind = load_bin("omx_visible_rebind")
    with pytest.raises(SystemExit):
        rebind.build_parser().parse_args(["discover"])
    result = rebind.rewrite_surface_text("- worker_id: `previous-worker`\n- port: `12345`\n", "selected-worker", "%99", 23456)
    assert "selected-worker" in result and "23456" in result
    assert "previous-worker" not in result and "12345" not in result
