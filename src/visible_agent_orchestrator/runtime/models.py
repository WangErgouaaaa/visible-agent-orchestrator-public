"""Small serializable models for runtime adapter evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import RuntimeAdapterError


@dataclass(frozen=True)
class RuntimeEvidence:
    source: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeCapability:
    name: str
    supported: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeBinding:
    worker_id: str
    backend: str
    backend_type: str = ""
    runtime_identity: str = ""
    session_id: str = ""
    window_id: str = ""
    pane_id: str = ""
    process_id: int | None = None
    port: int | None = None
    host: str = ""
    binding_confidence: str = ""
    evidence: list[RuntimeEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pane"] = self.pane_id
        return data


@dataclass(frozen=True)
class RuntimeWorker:
    worker_id: str
    backend: str
    role: str
    allow_auto_submit: bool
    default_for_new_tasks: bool
    backend_type: str = ""
    runtime_identity: str = ""
    session_id: str = ""
    window_id: str = ""
    pane_id: str = ""
    process_id: int | None = None
    port: int | None = None
    host: str = ""
    readiness: str = ""
    binding_confidence: str = ""
    capabilities: list[RuntimeCapability] = field(default_factory=list)
    evidence: list[RuntimeEvidence] = field(default_factory=list)
    dispatch_allowed: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pane"] = self.pane_id
        return data


@dataclass(frozen=True)
class RuntimeReadiness:
    worker_id: str
    backend: str
    readiness: str
    reason: str
    evidence: list[RuntimeEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolveResult:
    worker: RuntimeWorker
    binding: RuntimeBinding
    dispatch_allowed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker.to_dict(),
            "binding": self.binding.to_dict(),
            "dispatch_allowed": self.dispatch_allowed,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class VerifyResult:
    worker_id: str
    backend: str
    ok: bool
    dispatch_allowed: bool
    binding: RuntimeBinding | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: list[RuntimeEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "backend": self.backend,
            "ok": self.ok,
            "dispatch_allowed": self.dispatch_allowed,
            "binding": self.binding.to_dict() if self.binding else None,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class SubmitCapability:
    supports_submit_plan: bool
    supports_live_submit: bool
    live_submit_requires_explicit_allow_real: bool
    allowed_endpoint_kinds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubmitPlan:
    worker_id: str
    backend_name: str
    backend_type: str
    dispatch_allowed: bool
    submit_supported: bool
    submit_endpoint_kind: str
    target_host: str = ""
    target_port: int | None = None
    target_pane: str = ""
    required_api_paths: list[str] = field(default_factory=list)
    planned_actions: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    evidence: list[RuntimeEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["backend"] = self.backend_name
        data["supported"] = self.submit_supported
        data["reason"] = self.blocked_reason
        data["would_submit"] = False
        return data


__all__ = [
    "ResolveResult",
    "RuntimeAdapterError",
    "RuntimeBinding",
    "RuntimeCapability",
    "RuntimeEvidence",
    "RuntimeReadiness",
    "RuntimeWorker",
    "SubmitCapability",
    "SubmitPlan",
    "VerifyResult",
]
