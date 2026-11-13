"""Fixture-only worker agent backend implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError

from .adapter import WorkerBackend
from .models import (
    WorkerAgent,
    WorkerBackendCapability,
    WorkerBackendEvidence,
    WorkerReadiness,
    WorkerResolveResult,
    WorkerSubmitPlan,
    WorkerVerifyResult,
)


class FixtureWorkerBackend(WorkerBackend):
    """Read worker backend candidates from fixture JSON only."""

    fixture_filename = "worker-backends.json"

    def __init__(self, *, backend_name: str, agent_kind: str, fixture_dir: Path | None = None) -> None:
        self.backend_name = backend_name
        self.agent_kind = agent_kind
        self.fixture_dir = fixture_dir

    def _fixture_path(self) -> Path:
        if self.fixture_dir is None:
            raise RuntimeAdapterError(
                f"{self.backend_name} fixture required; live {self.agent_kind} worker backend is disabled"
            )
        path = self.fixture_dir / self.fixture_filename
        if not path.is_file():
            raise RuntimeAdapterError(f"{self.backend_name} fixture file missing: {path}")
        return path

    def _payload(self) -> dict[str, Any]:
        path = self._fixture_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError(f"{self.backend_name} fixture is invalid JSON: {exc}") from exc
        if payload.get("backend") != self.backend_name:
            raise RuntimeAdapterError(
                f"fixture backend mismatch: expected {self.backend_name}, got {payload.get('backend')}"
            )
        if payload.get("fixture_only") is not True:
            raise RuntimeAdapterError(f"{self.backend_name} fixture must set fixture_only=true")
        if payload.get("live_enabled") is not False:
            raise RuntimeAdapterError(f"{self.backend_name} fixture must set live_enabled=false")
        return payload

    def _worker_from_raw(self, raw: dict[str, Any]) -> WorkerAgent:
        capabilities = [
            WorkerBackendCapability(
                name=str(item.get("name", "")),
                supported=bool(item.get("supported", False)),
                detail=str(item.get("detail", "")),
            )
            for item in raw.get("capabilities", [])
        ]
        evidence = [
            WorkerBackendEvidence("fixture", {"path": str(self._fixture_path()), "worker_id": raw.get("worker_id")})
        ]
        warnings = list(raw.get("warnings", []))
        warnings.append(f"{self.backend_name} is fixture-only; no live {self.agent_kind} process is contacted")
        return WorkerAgent(
            worker_id=str(raw.get("worker_id", "")),
            backend=self.backend_name,
            agent_kind=self.agent_kind,
            role=str(raw.get("role", "")),
            fixture_only=True,
            live_enabled=False,
            runtime_backend=str(raw.get("runtime_backend", "unassigned")),
            dispatch_adapter=str(raw.get("dispatch_adapter", "")),
            allow_live_submit=False,
            default_for_new_tasks=bool(raw.get("default_for_new_tasks", False)),
            readiness=str(raw.get("readiness", "unknown")),
            receipt_protocol=str(raw.get("receipt_protocol", "visible-run-report-marker")),
            capabilities=capabilities,
            evidence=evidence,
            warnings=warnings,
            errors=list(raw.get("errors", [])),
            raw=raw,
        )

    def discover_workers(self, root: Path) -> list[WorkerAgent]:
        payload = self._payload()
        return [self._worker_from_raw(item) for item in payload.get("workers", [])]

    def resolve_worker(self, root: Path, worker_id: str) -> WorkerResolveResult:
        matches = [worker for worker in self.discover_workers(root) if worker.worker_id == worker_id]
        if not matches:
            raise RuntimeAdapterError(f"{self.backend_name} worker not found: {worker_id}")
        if len(matches) > 1:
            raise RuntimeAdapterError(f"ambiguous {self.backend_name} worker: {worker_id}")
        worker = matches[0]
        warnings = list(worker.warnings)
        errors = list(worker.errors)
        errors.append(f"{self.backend_name} fixture-only backend has live submit disabled")
        return WorkerResolveResult(worker=worker, dispatch_allowed=False, warnings=warnings, errors=errors)

    def verify_worker(self, root: Path, worker_id: str) -> WorkerVerifyResult:
        result = self.resolve_worker(root, worker_id)
        errors = [item for item in result.errors if "fixture-only backend has live submit disabled" not in item]
        worker = result.worker
        if worker.agent_kind != self.agent_kind:
            errors.append(f"agent_kind mismatch: expected {self.agent_kind}, got {worker.agent_kind}")
        if not worker.fixture_only:
            errors.append("fixture_only must be true")
        if worker.live_enabled:
            errors.append("live_enabled must be false")
        return WorkerVerifyResult(
            worker_id=worker.worker_id,
            backend=self.backend_name,
            ok=not errors,
            dispatch_allowed=False,
            warnings=list(result.warnings),
            errors=errors,
            evidence=list(worker.evidence),
        )

    def fetch_readiness(self, root: Path, worker_id: str) -> WorkerReadiness:
        result = self.resolve_worker(root, worker_id)
        worker = result.worker
        return WorkerReadiness(
            worker_id=worker.worker_id,
            backend=self.backend_name,
            readiness=worker.readiness,
            reason=f"fixture-only {self.agent_kind} readiness; not protocol terminal truth",
            evidence=list(worker.evidence),
            warnings=list(result.warnings),
        )

    def submit_plan(self, root: Path, worker_id: str, task_reference: dict[str, Any] | None = None) -> WorkerSubmitPlan:
        result = self.resolve_worker(root, worker_id)
        worker = result.worker
        return WorkerSubmitPlan(
            worker_id=worker.worker_id,
            backend=self.backend_name,
            agent_kind=self.agent_kind,
            dispatch_allowed=False,
            submit_supported=False,
            fixture_only=True,
            planned_actions=["read_fixture", "refuse_live_submit", "preserve_existing_live_runtime"],
            blocked_reason=f"{self.backend_name} is fixture-only; live {self.agent_kind} submit is not enabled",
            warnings=list(result.warnings),
            evidence=list(worker.evidence),
        )


class ClaudeCodeFixtureBackend(FixtureWorkerBackend):
    def __init__(self, fixture_dir: Path | None = None) -> None:
        super().__init__(backend_name="claude-code-fixture", agent_kind="claude-code", fixture_dir=fixture_dir)


class CursorAgentFixtureBackend(FixtureWorkerBackend):
    def __init__(self, fixture_dir: Path | None = None) -> None:
        super().__init__(backend_name="cursor-agent-fixture", agent_kind="cursor-agent", fixture_dir=fixture_dir)
