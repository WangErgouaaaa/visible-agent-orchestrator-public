"""RuntimeAdapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .models import ResolveResult, RuntimeReadiness, RuntimeWorker, SubmitCapability, SubmitPlan, VerifyResult


class RuntimeAdapter(ABC):
    """Substrate-neutral interface for runtime evidence."""

    backend_name = "unknown"

    @abstractmethod
    def discover_workers(self, root: Path) -> list[RuntimeWorker]:
        """Discover known runtime workers without mutating runtime state."""

    @abstractmethod
    def resolve_worker(self, root: Path, worker_id: str) -> ResolveResult:
        """Resolve one logical worker id to runtime binding evidence."""

    @abstractmethod
    def verify_binding(self, root: Path, worker_id: str) -> VerifyResult:
        """Verify a binding without repairing, submitting, or rebinding."""

    @abstractmethod
    def fetch_readiness(self, root: Path, worker_id: str) -> RuntimeReadiness:
        """Fetch runtime readiness telemetry."""

    @abstractmethod
    def observe_status(self, root: Path, worker_id: str) -> dict[str, Any]:
        """Return backend status telemetry."""

    @abstractmethod
    def collect_runtime_metadata(self, root: Path, worker_id: str) -> dict[str, Any]:
        """Return runtime identity and capability metadata."""

    @abstractmethod
    def submit_task(self, root: Path, worker_id: str, task_reference: dict[str, Any]) -> SubmitPlan:
        """Plan or perform submission through the selected backend."""

    @abstractmethod
    def submit_plan(self, root: Path, worker_id: str, task_reference: dict[str, Any] | None = None) -> SubmitPlan:
        """Return a machine-readable submission plan without mutating runtime state."""

    @abstractmethod
    def submit_capability(self) -> SubmitCapability:
        """Return backend submission capability metadata."""
