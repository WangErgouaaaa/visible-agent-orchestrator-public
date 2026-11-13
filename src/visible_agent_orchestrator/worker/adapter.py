"""WorkerBackend interface.

Worker backends are agent-family adapters. They are intentionally separate from
RuntimeAdapter substrate backends such as tmux-reference or rmux-spike.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.errors import UnsupportedRuntimeOperation

from .models import WorkerAgent, WorkerReadiness, WorkerResolveResult, WorkerSubmitPlan, WorkerVerifyResult


class WorkerBackend(ABC):
    """Agent-backend interface for fixture evidence and future protocol routing."""

    backend_name = "unknown"
    agent_kind = "unknown"

    @abstractmethod
    def discover_workers(self, root: Path) -> list[WorkerAgent]:
        """Discover configured worker agents without contacting live agents."""

    @abstractmethod
    def resolve_worker(self, root: Path, worker_id: str) -> WorkerResolveResult:
        """Resolve one worker from backend evidence."""

    @abstractmethod
    def verify_worker(self, root: Path, worker_id: str) -> WorkerVerifyResult:
        """Verify fixture evidence for a worker without enabling live submit."""

    @abstractmethod
    def fetch_readiness(self, root: Path, worker_id: str) -> WorkerReadiness:
        """Return readiness telemetry; this is not protocol terminal truth."""

    @abstractmethod
    def submit_plan(self, root: Path, worker_id: str, task_reference: dict[str, Any] | None = None) -> WorkerSubmitPlan:
        """Return a non-mutating submission plan."""

    def submit_task(self, root: Path, worker_id: str, task_reference: dict[str, Any]) -> WorkerSubmitPlan:
        """Fail closed until a future explicit live adapter is authorized."""

        raise UnsupportedRuntimeOperation(
            f"{self.backend_name} live submit is disabled; use fixture-only planning until explicitly authorized"
        )
