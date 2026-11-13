"""Serializable models for worker agent backend evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkerBackendEvidence:
    source: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerBackendCapability:
    name: str
    supported: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerAgent:
    worker_id: str
    backend: str
    agent_kind: str
    role: str
    fixture_only: bool
    live_enabled: bool
    runtime_backend: str = ""
    dispatch_adapter: str = ""
    allow_live_submit: bool = False
    default_for_new_tasks: bool = False
    readiness: str = "unknown"
    receipt_protocol: str = "visible-run-report-marker"
    capabilities: list[WorkerBackendCapability] = field(default_factory=list)
    evidence: list[WorkerBackendEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dispatch_allowed(self) -> bool:
        return self.live_enabled and self.allow_live_submit and not self.fixture_only

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dispatch_allowed"] = self.dispatch_allowed
        return data


@dataclass(frozen=True)
class WorkerResolveResult:
    worker: WorkerAgent
    dispatch_allowed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker.to_dict(),
            "dispatch_allowed": self.dispatch_allowed,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class WorkerVerifyResult:
    worker_id: str
    backend: str
    ok: bool
    dispatch_allowed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: list[WorkerBackendEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "backend": self.backend,
            "ok": self.ok,
            "dispatch_allowed": self.dispatch_allowed,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class WorkerReadiness:
    worker_id: str
    backend: str
    readiness: str
    reason: str
    evidence: list[WorkerBackendEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerSubmitPlan:
    worker_id: str
    backend: str
    agent_kind: str
    dispatch_allowed: bool
    submit_supported: bool
    fixture_only: bool
    planned_actions: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    evidence: list[WorkerBackendEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["supported"] = self.submit_supported
        data["would_submit"] = False
        data["reason"] = self.blocked_reason
        return data


__all__ = [
    "WorkerAgent",
    "WorkerBackendCapability",
    "WorkerBackendEvidence",
    "WorkerReadiness",
    "WorkerResolveResult",
    "WorkerSubmitPlan",
    "WorkerVerifyResult",
]
