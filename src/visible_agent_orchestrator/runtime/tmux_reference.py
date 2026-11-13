"""tmux-reference RuntimeAdapter implementation.

The adapter is intentionally read-only. It resolves registry-backed worker
metadata and can optionally inspect fixture or live read-only command output.
It never sends tmux input, calls OpenCode submit endpoints, or starts rmux.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .adapter import RuntimeAdapter
from .errors import RegistryError, UnsupportedRuntimeOperation
from .models import (
    ResolveResult,
    RuntimeBinding,
    RuntimeEvidence,
    RuntimeReadiness,
    RuntimeWorker,
    SubmitCapability,
    SubmitPlan,
    VerifyResult,
)
from .registry import (
    load_visible_workers,
    resolve_worker_record,
    validate_worker_for_dispatch,
)
from .tmux_capabilities import command_support_report


class TmuxReferenceAdapter(RuntimeAdapter):
    """Read-only tmux-reference backend."""

    backend_name = "tmux-reference"

    def __init__(self, *, fixture_dir: Path | None = None, live_probe: bool = False) -> None:
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.live_probe = live_probe

    def discover_workers(self, root: Path) -> list[RuntimeWorker]:
        return [self._worker_from_record(record) for record in load_visible_workers(root, self.fixture_dir)]

    def resolve_worker(self, root: Path, worker_id: str) -> ResolveResult:
        record = resolve_worker_record(root, worker_id, self.fixture_dir)
        worker = self._worker_from_record(record)
        binding = self._binding_from_record(record)
        validation = validate_worker_for_dispatch(record, must_be_default=bool(record.get("default_for_new_tasks")))
        warnings = list(worker.warnings) + list(validation["warnings"])
        errors = list(worker.errors) + list(validation["errors"])
        return ResolveResult(
            worker=worker,
            binding=binding,
            dispatch_allowed=not errors,
            warnings=warnings,
            errors=errors,
        )

    def verify_binding(self, root: Path, worker_id: str) -> VerifyResult:
        try:
            record = resolve_worker_record(root, worker_id, self.fixture_dir)
        except RegistryError as exc:
            return VerifyResult(
                worker_id=worker_id,
                backend=self.backend_name,
                ok=False,
                dispatch_allowed=False,
                errors=[str(exc)],
                evidence=[RuntimeEvidence("registry", {"error": str(exc)})],
            )
        validation = validate_worker_for_dispatch(record, must_be_default=False)
        binding = self._binding_from_record(record)
        evidence = [RuntimeEvidence("registry", {"worker_id": record.get("worker_id")})]
        warnings = list(validation["warnings"])
        errors = list(validation["errors"])
        if str(record.get("binding_confidence") or "") != "strong":
            warnings.append("binding is not strong; runtime adapter reports stale or weak binding evidence")
        if self.fixture_dir:
            evidence.extend(self._fixture_evidence())
        elif self.live_probe:
            evidence.extend(self._live_tmux_evidence())
        return VerifyResult(
            worker_id=str(record.get("worker_id") or worker_id),
            backend=self.backend_name,
            ok=not errors,
            dispatch_allowed=not errors,
            binding=binding,
            warnings=warnings,
            errors=errors,
            evidence=evidence,
        )

    def fetch_readiness(self, root: Path, worker_id: str) -> RuntimeReadiness:
        record = resolve_worker_record(root, worker_id, self.fixture_dir)
        return RuntimeReadiness(
            worker_id=str(record.get("worker_id") or worker_id),
            backend=self.backend_name,
            readiness="unknown",
            reason=(
                "tmux-reference readiness is telemetry only; this adapter does "
                "not call the OpenCode TUI server"
            ),
            evidence=[RuntimeEvidence("registry", {"worker_id": record.get("worker_id")})],
            warnings=["runtime readiness is not protocol terminal truth"],
        )

    def observe_status(self, root: Path, worker_id: str) -> dict[str, Any]:
        record = resolve_worker_record(root, worker_id, self.fixture_dir)
        return {
            "backend": self.backend_name,
            "worker_id": record.get("worker_id"),
            "readiness": "unknown",
            "reason": "read-only registry-backed status; no OpenCode call performed",
            "binding": self._binding_from_record(record).to_dict(),
        }

    def collect_runtime_metadata(self, root: Path, worker_id: str) -> dict[str, Any]:
        record = resolve_worker_record(root, worker_id, self.fixture_dir)
        return {
            "backend": self.backend_name,
            "worker_id": record.get("worker_id"),
            "pane_id": record.get("pane_id"),
            "pane": record.get("pane_id"),
            "port": record.get("port"),
            "host": record.get("host"),
            "binding_confidence": record.get("binding_confidence"),
            "fixture_dir": str(self.fixture_dir) if self.fixture_dir else "",
            "live_probe": self.live_probe,
            **command_support_report(),
            "capabilities": [
                {"name": "discover_workers", "supported": True},
                {"name": "resolve_worker", "supported": True},
                {"name": "verify_binding", "supported": True},
                {"name": "fetch_readiness", "supported": True},
                self.submit_capability().to_dict(),
            ],
        }

    def submit_task(self, root: Path, worker_id: str, task_reference: dict[str, Any]) -> SubmitPlan:
        raise UnsupportedRuntimeOperation(
            "tmux-reference adapter does not submit tasks; use the visible dispatch control surface"
        )

    def submit_plan(self, root: Path, worker_id: str, task_reference: dict[str, Any] | None = None) -> SubmitPlan:
        requested = str(worker_id or "").strip()
        try:
            resolved = self.resolve_worker(root, requested)
            verified = self.verify_binding(root, requested)
        except RegistryError as exc:
            return SubmitPlan(
                worker_id=requested,
                backend_name=self.backend_name,
                backend_type="tmux",
                dispatch_allowed=False,
                submit_supported=False,
                submit_endpoint_kind="opencode_tui_api",
                planned_actions=["generate_submit_plan", "no_http_post", "no_marker_write"],
                blocked_reason=str(exc),
                evidence=[RuntimeEvidence("registry", {"error": str(exc)})],
            )
        errors = list(resolved.errors) + list(verified.errors)
        warnings = list(resolved.warnings) + list(verified.warnings)
        allowed = bool(resolved.dispatch_allowed and verified.ok and verified.dispatch_allowed and not errors)
        return SubmitPlan(
            worker_id=resolved.worker.worker_id,
            backend_name=self.backend_name,
            backend_type="tmux",
            dispatch_allowed=allowed,
            submit_supported=allowed,
            submit_endpoint_kind="opencode_tui_api",
            target_host=resolved.binding.host,
            target_port=resolved.binding.port,
            target_pane=resolved.binding.pane_id,
            required_api_paths=["/tui/clear-prompt", "/tui/append-prompt", "/tui/submit-prompt"],
            planned_actions=["generate_submit_plan", "no_http_post", "no_marker_write"],
            blocked_reason="; ".join(errors),
            warnings=warnings,
            evidence=[RuntimeEvidence("runtime-adapter", {"source": "tmux-reference submit_plan"})],
        )

    def submit_capability(self) -> SubmitCapability:
        return SubmitCapability(
            supports_submit_plan=True,
            supports_live_submit=True,
            live_submit_requires_explicit_allow_real=True,
            allowed_endpoint_kinds=["opencode_tui_api"],
        )

    def _worker_from_record(self, record: dict[str, Any]) -> RuntimeWorker:
        validation = validate_worker_for_dispatch(record, must_be_default=False)
        worker = validation["worker"]
        return RuntimeWorker(
            worker_id=str(worker.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type="tmux",
            role=str(worker.get("role") or ""),
            allow_auto_submit=bool(worker.get("allow_auto_submit")),
            default_for_new_tasks=bool(worker.get("default_for_new_tasks")),
            pane_id=str(worker.get("pane_id") or ""),
            port=worker.get("port"),
            host=str(worker.get("host") or ""),
            binding_confidence=str(worker.get("binding_confidence") or ""),
            dispatch_allowed=bool(validation["dispatch_allowed"]),
            warnings=list(validation["warnings"]),
            errors=list(validation["errors"]),
            raw=worker,
        )

    def _binding_from_record(self, record: dict[str, Any]) -> RuntimeBinding:
        return RuntimeBinding(
            worker_id=str(record.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type="tmux",
            pane_id=str(record.get("pane_id") or record.get("pane") or ""),
            port=record.get("port"),
            host=str(record.get("host") or ""),
            binding_confidence=str(record.get("binding_confidence") or ""),
            evidence=[RuntimeEvidence("registry", {"path": "opencode_visible_workers.json"})],
            warnings=[] if str(record.get("binding_confidence") or "") == "strong" else ["binding is not strong"],
        )

    def _fixture_evidence(self) -> list[RuntimeEvidence]:
        assert self.fixture_dir is not None
        evidence: list[RuntimeEvidence] = []
        for name in ("tmux-list-panes.txt", "ss-listen.txt", "lsof-listen.txt"):
            path = self.fixture_dir / name
            if path.is_file():
                evidence.append(RuntimeEvidence(name, {"path": str(path), "bytes": path.stat().st_size}))
        return evidence

    def _live_tmux_evidence(self) -> list[RuntimeEvidence]:
        commands = [
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid} #{session_name}:#{window_index}.#{pane_index}"],
        ]
        evidence: list[RuntimeEvidence] = []
        for argv in commands:
            try:
                result = subprocess.run(argv, text=True, capture_output=True, timeout=5, check=False)
            except Exception as exc:
                evidence.append(RuntimeEvidence("live-readonly", {"argv": argv, "error": str(exc)}))
                continue
            evidence.append(
                RuntimeEvidence(
                    "live-readonly",
                    {
                        "argv": argv,
                        "returncode": result.returncode,
                        "stdout_preview": result.stdout[:1000],
                        "stderr_preview": result.stderr[:1000],
                    },
                )
            )
        return evidence
