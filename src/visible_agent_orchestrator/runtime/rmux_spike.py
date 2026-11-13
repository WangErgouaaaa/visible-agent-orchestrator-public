"""Fixture-only rmux RuntimeAdapter skeleton.

Spike-1 deliberately refuses live rmux access. The adapter reads fixture files
that model rmux sessions, windows, panes, and snapshots so the upper
RuntimeAdapter contract can be exercised without creating runtime state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapter import RuntimeAdapter
from .errors import RuntimeAdapterError, UnsupportedRuntimeOperation
from .models import (
    ResolveResult,
    RuntimeBinding,
    RuntimeCapability,
    RuntimeEvidence,
    RuntimeReadiness,
    RuntimeWorker,
    SubmitCapability,
    SubmitPlan,
    VerifyResult,
)


class RmuxSpikeAdapter(RuntimeAdapter):
    """Fixture-only candidate backend for rmux substrate evidence."""

    backend_name = "rmux-spike"
    backend_type = "rmux"

    def __init__(self, *, fixture_dir: Path | None = None, live_probe: bool = False) -> None:
        self.fixture_dir = Path(fixture_dir).resolve() if fixture_dir else None
        self.live_probe = live_probe

    def discover_workers(self, root: Path) -> list[RuntimeWorker]:
        data = self._fixture_data()
        return [self._worker_from_record(record) for record in self._records(data)]

    def resolve_worker(self, root: Path, worker_id: str) -> ResolveResult:
        record = self._resolve_record(worker_id)
        worker = self._worker_from_record(record)
        binding = self._binding_from_record(record)
        warnings = list(worker.warnings)
        errors = list(worker.errors)
        return ResolveResult(
            worker=worker,
            binding=binding,
            dispatch_allowed=False,
            warnings=warnings,
            errors=errors,
        )

    def verify_binding(self, root: Path, worker_id: str) -> VerifyResult:
        try:
            record = self._resolve_record(worker_id)
        except RuntimeAdapterError as exc:
            return VerifyResult(
                worker_id=worker_id,
                backend=self.backend_name,
                ok=False,
                dispatch_allowed=False,
                errors=[str(exc)],
                evidence=[RuntimeEvidence("fixture", {"error": str(exc)})],
            )

        binding = self._binding_from_record(record)
        errors = self._binding_errors(record)
        warnings = self._binding_warnings(record)
        evidence = self._evidence_from_record(record)
        return VerifyResult(
            worker_id=str(record.get("worker_id") or worker_id),
            backend=self.backend_name,
            ok=not errors,
            dispatch_allowed=False,
            binding=binding,
            warnings=warnings,
            errors=errors,
            evidence=evidence,
        )

    def fetch_readiness(self, root: Path, worker_id: str) -> RuntimeReadiness:
        record = self._resolve_record(worker_id)
        readiness = str(record.get("readiness") or "unknown")
        if readiness not in {"ready", "busy", "unknown", "error"}:
            readiness = "unknown"
        return RuntimeReadiness(
            worker_id=str(record.get("worker_id") or worker_id),
            backend=self.backend_name,
            readiness=readiness,
            reason=(
                "rmux-spike readiness is fixture telemetry only; it is not "
                "protocol terminal truth"
            ),
            evidence=self._evidence_from_record(record),
            warnings=["runtime readiness is not protocol terminal truth"],
        )

    def observe_status(self, root: Path, worker_id: str) -> dict[str, Any]:
        record = self._resolve_record(worker_id)
        snapshot = self._snapshot_for_record(record)
        return {
            "backend": self.backend_name,
            "backend_type": self.backend_type,
            "worker_id": record.get("worker_id"),
            "session_id": record.get("session_id"),
            "window_id": record.get("window_id"),
            "pane_id": record.get("pane_id"),
            "process_id": record.get("process_id"),
            "status": snapshot.get("status", "unknown"),
            "readiness": snapshot.get("readiness", record.get("readiness", "unknown")),
            "evidence": [item.to_dict() for item in self._evidence_from_record(record)],
            "terminal_truth": "reports and markers remain owned by the upper protocol",
        }

    def collect_runtime_metadata(self, root: Path, worker_id: str) -> dict[str, Any]:
        data = self._fixture_data()
        record = self._resolve_record(worker_id)
        return {
            "backend": self.backend_name,
            "backend_type": self.backend_type,
            "worker_id": record.get("worker_id"),
            "fixture_dir": str(self.fixture_dir) if self.fixture_dir else "",
            "fixture_mode": True,
            "live_enabled": False,
            "rmux_default": False,
            "rmux_version": data.get("rmux_version", ""),
            "help_probe_summary": data.get("help_probe_summary", ""),
            "capabilities": [cap.to_dict() for cap in self._capabilities(record)],
        }

    def submit_task(self, root: Path, worker_id: str, task_reference: dict[str, Any]) -> SubmitPlan:
        raise UnsupportedRuntimeOperation(
            "rmux live submit requires explicit rmux-backend-live-sandbox authorization"
        )

    def submit_plan(self, root: Path, worker_id: str, task_reference: dict[str, Any] | None = None) -> SubmitPlan:
        return SubmitPlan(
            worker_id=worker_id,
            backend_name=self.backend_name,
            backend_type=self.backend_type,
            dispatch_allowed=False,
            submit_supported=False,
            submit_endpoint_kind="unsupported",
            planned_actions=["generate_submit_plan", "no_rmux_command", "no_http_post", "no_marker_write"],
            blocked_reason=(
                "rmux live submit not implemented; rmux live submit requires explicit "
                "rmux-backend-live-sandbox authorization"
            ),
            warnings=["rmux-spike is fixture-only; live dispatch disabled"],
            evidence=[RuntimeEvidence("rmux-spike", {"fixture_only": True, "live_enabled": False})],
        )

    def submit_capability(self) -> SubmitCapability:
        return SubmitCapability(
            supports_submit_plan=True,
            supports_live_submit=False,
            live_submit_requires_explicit_allow_real=True,
            allowed_endpoint_kinds=["unsupported"],
        )

    def _fixture_data(self) -> dict[str, Any]:
        if self.live_probe:
            raise RuntimeAdapterError("rmux live backend not authorized in spike-1")
        if self.fixture_dir is None:
            raise RuntimeAdapterError("rmux-spike fixture required; live rmux disabled in spike-1")
        path = self.fixture_dir / "rmux-workers.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeAdapterError(f"rmux fixture not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError(f"rmux fixture is invalid JSON: {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeAdapterError(f"rmux fixture must be a JSON object: {path}")
        return data

    def _records(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        workers = data.get("workers", [])
        if not isinstance(workers, list):
            raise RuntimeAdapterError("rmux fixture field 'workers' must be a list")
        records: list[dict[str, Any]] = []
        for item in workers:
            if isinstance(item, dict):
                records.append(self._normalize_record(item))
        return records

    def _resolve_record(self, worker_id: str) -> dict[str, Any]:
        requested = str(worker_id or "").strip()
        matches = [record for record in self._records(self._fixture_data()) if record.get("worker_id") == requested]
        if not matches:
            raise RuntimeAdapterError(f"rmux worker not found: {requested}")
        identities = sorted({str(item.get("runtime_identity") or "") for item in matches})
        if len(matches) > 1 or len(identities) > 1:
            raise RuntimeAdapterError(f"ambiguous rmux worker: {requested}")
        return matches[0]

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        worker = dict(record)
        worker["worker_id"] = str(worker.get("worker_id") or "")
        worker["runtime_identity"] = str(
            worker.get("runtime_identity")
            or f"{worker.get('session_id') or ''}:{worker.get('window_id') or ''}.{worker.get('pane_id') or ''}"
        )
        worker["role"] = str(worker.get("role") or "production_candidate")
        worker["allow_auto_submit"] = self._coerce_bool(worker.get("allow_auto_submit"))
        worker["default_for_new_tasks"] = self._coerce_bool(worker.get("default_for_new_tasks"))
        worker["session_id"] = str(worker.get("session_id") or "")
        worker["window_id"] = str(worker.get("window_id") or "")
        worker["pane_id"] = str(worker.get("pane_id") or worker.get("pane") or "")
        worker["pane"] = worker["pane_id"]
        worker["process_id"] = self._coerce_int(worker.get("process_id"))
        worker["readiness"] = str(worker.get("readiness") or "unknown")
        worker["binding_confidence"] = str(worker.get("binding_confidence") or "")
        capabilities = worker.get("capabilities")
        worker["capabilities"] = capabilities if isinstance(capabilities, list) else []
        evidence = worker.get("evidence")
        worker["evidence"] = evidence if isinstance(evidence, dict) else {}
        return worker

    def _worker_from_record(self, record: dict[str, Any]) -> RuntimeWorker:
        errors = self._dispatch_errors(record)
        warnings = ["rmux-spike is fixture-only; live dispatch disabled"]
        warnings.extend(self._binding_warnings(record))
        return RuntimeWorker(
            worker_id=str(record.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type=self.backend_type,
            runtime_identity=str(record.get("runtime_identity") or ""),
            role=str(record.get("role") or ""),
            allow_auto_submit=bool(record.get("allow_auto_submit")),
            default_for_new_tasks=bool(record.get("default_for_new_tasks")),
            session_id=str(record.get("session_id") or ""),
            window_id=str(record.get("window_id") or ""),
            pane_id=str(record.get("pane_id") or ""),
            process_id=record.get("process_id"),
            readiness=str(record.get("readiness") or "unknown"),
            binding_confidence=str(record.get("binding_confidence") or ""),
            capabilities=self._capabilities(record),
            evidence=self._evidence_from_record(record),
            dispatch_allowed=False,
            warnings=warnings,
            errors=errors,
            raw=record,
        )

    def _binding_from_record(self, record: dict[str, Any]) -> RuntimeBinding:
        return RuntimeBinding(
            worker_id=str(record.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type=self.backend_type,
            runtime_identity=str(record.get("runtime_identity") or ""),
            session_id=str(record.get("session_id") or ""),
            window_id=str(record.get("window_id") or ""),
            pane_id=str(record.get("pane_id") or ""),
            process_id=record.get("process_id"),
            binding_confidence=str(record.get("binding_confidence") or ""),
            evidence=self._evidence_from_record(record),
            warnings=self._binding_warnings(record),
        )

    def _dispatch_errors(self, record: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        worker_id = str(record.get("worker_id") or "")
        role = str(record.get("role") or "")
        if role in {"test", "disabled"}:
            errors.append(f"worker {worker_id} is not dispatchable: role={role}")
        if not bool(record.get("allow_auto_submit")):
            errors.append(f"worker {worker_id} has allow_auto_submit=false")
        if str(record.get("binding_confidence") or "") != "strong":
            errors.append(
                f"worker {worker_id} binding_confidence is not strong: {record.get('binding_confidence') or '-'}"
            )
        return errors

    def _binding_errors(self, record: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        worker_id = str(record.get("worker_id") or "")
        if str(record.get("binding_confidence") or "") != "strong":
            errors.append(
                f"worker {worker_id} binding_confidence is not strong: {record.get('binding_confidence') or '-'}"
            )
        for field in ("session_id", "window_id", "pane_id"):
            if not record.get(field):
                errors.append(f"worker {worker_id} missing {field}")
        if record.get("process_id") is None:
            errors.append(f"worker {worker_id} missing process_id")
        return errors

    def _binding_warnings(self, record: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        if str(record.get("binding_confidence") or "") != "strong":
            warnings.append("binding is not strong; rmux fixture reports stale or weak evidence")
        return warnings

    def _capabilities(self, record: dict[str, Any]) -> list[RuntimeCapability]:
        names = [str(item) for item in record.get("capabilities", []) if str(item)]
        capabilities = [
            RuntimeCapability(name=name, supported=True, detail="fixture-backed") for name in names
        ]
        capabilities.append(
            RuntimeCapability(
                name="submit_plan",
                supported=True,
                detail="fixture-only planning metadata; live submit disabled",
            )
        )
        capabilities.append(
            RuntimeCapability(
                name="submit_task",
                supported=False,
                detail="rmux live submit requires explicit live sandbox authorization",
            )
        )
        return capabilities

    def _evidence_from_record(self, record: dict[str, Any]) -> list[RuntimeEvidence]:
        evidence = record.get("evidence", {})
        detail = dict(evidence) if isinstance(evidence, dict) else {}
        detail.update(
            {
                "fixture_dir": str(self.fixture_dir) if self.fixture_dir else "",
                "runtime_identity": record.get("runtime_identity", ""),
                "session_id": record.get("session_id", ""),
                "window_id": record.get("window_id", ""),
                "pane_id": record.get("pane_id", ""),
                "process_id": record.get("process_id"),
            }
        )
        return [RuntimeEvidence("fixture", detail)]

    def _snapshot_for_record(self, record: dict[str, Any]) -> dict[str, Any]:
        evidence = record.get("evidence", {})
        filename = evidence.get("snapshot_file") if isinstance(evidence, dict) else None
        if not filename or self.fixture_dir is None:
            return {}
        path = self.fixture_dir / str(filename)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
