"""rmux-reference RuntimeAdapter implementation.

This backend keeps the current artifact/report truth model, but exposes a
real rmux substrate label for live registry-backed workers and for rmux
fixtures. It does not perform native rmux submit yet; submit continues to use
the existing OpenCode TUI API path.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import RuntimeAdapterError
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
from .registry import (
    load_visible_workers,
    resolve_worker_record,
    validate_worker_for_dispatch,
)
from .tmux_reference import TmuxReferenceAdapter


class RmuxReferenceAdapter(TmuxReferenceAdapter):
    """Registry-backed rmux live backend plus rmux fixture support."""

    backend_name = "rmux-reference"
    required_tui_paths = ("/tui/clear-prompt", "/tui/append-prompt", "/tui/submit-prompt")

    def discover_workers(self, root: Path) -> list[RuntimeWorker]:
        if self._has_rmux_fixture():
            return [self._fixture_worker_from_record(record) for record in self._fixture_records()]
        return super().discover_workers(root)

    def resolve_worker(self, root: Path, worker_id: str) -> ResolveResult:
        if self._has_rmux_fixture():
            record = self._resolve_fixture_record(worker_id)
            worker = self._fixture_worker_from_record(record)
            binding = self._fixture_binding_from_record(record)
            return ResolveResult(
                worker=worker,
                binding=binding,
                dispatch_allowed=not worker.errors,
                warnings=list(worker.warnings),
                errors=list(worker.errors),
            )
        return super().resolve_worker(root, worker_id)

    def verify_binding(self, root: Path, worker_id: str) -> VerifyResult:
        if self._has_rmux_fixture():
            try:
                record = self._resolve_fixture_record(worker_id)
            except RuntimeAdapterError as exc:
                return VerifyResult(
                    worker_id=worker_id,
                    backend=self.backend_name,
                    ok=False,
                    dispatch_allowed=False,
                    errors=[str(exc)],
                    evidence=[RuntimeEvidence("fixture", {"error": str(exc)})],
                )
            binding = self._fixture_binding_from_record(record)
            warnings = list(binding.warnings)
            errors = [] if str(record.get("binding_confidence") or "") == "strong" else [
                f"rmux worker {worker_id} binding_confidence is not strong: {record.get('binding_confidence') or '-'}"
            ]
            return VerifyResult(
                worker_id=str(record.get("worker_id") or worker_id),
                backend=self.backend_name,
                ok=not errors,
                dispatch_allowed=not errors,
                binding=binding,
                warnings=warnings,
                errors=errors,
                evidence=binding.evidence,
            )
        result = super().verify_binding(root, worker_id)
        if not result.ok or result.binding is None:
            return result
        pane_probe = self._live_pane_probe(result.binding)
        probe = self._live_probe_binding(result.binding)
        evidence = list(result.evidence) + [
            RuntimeEvidence("live-pane-probe", pane_probe),
            RuntimeEvidence("live-probe", probe),
        ]
        warnings = list(result.warnings)
        errors = list(result.errors)
        if pane_probe.get("error"):
            errors.append(
                f"live rmux worker {worker_id} pane probe failed: {pane_probe.get('error')}"
            )
        elif not pane_probe.get("pane_exists"):
            errors.append(
                f"live rmux worker {worker_id} registry pane {result.binding.pane_id or '-'} is not present in current rmux panes"
            )
        elif not pane_probe.get("opencode_process_present"):
            errors.append(
                f"live rmux worker {worker_id} pane {result.binding.pane_id or '-'} process tree does not contain opencode"
            )
        if not probe.get("health_ok") or not probe.get("doc_ok"):
            errors.append(
                f"live rmux worker {worker_id} is not reachable with /global/health and /doc"
            )
        else:
            missing = [path for path in self.required_tui_paths if path not in set(probe.get("detected_tui_methods", []))]
            if missing:
                errors.append(
                    f"live rmux worker {worker_id} /doc missing required endpoint(s): {', '.join(missing)}"
                )
        return VerifyResult(
            worker_id=result.worker_id,
            backend=result.backend,
            ok=not errors,
            dispatch_allowed=not errors,
            binding=result.binding,
            warnings=warnings,
            errors=errors,
            evidence=evidence,
        )

    def fetch_readiness(self, root: Path, worker_id: str) -> RuntimeReadiness:
        if self._has_rmux_fixture():
            record = self._resolve_fixture_record(worker_id)
            readiness = str(record.get("readiness") or "unknown")
            if readiness not in {"ready", "busy", "unknown", "error"}:
                readiness = "unknown"
            return RuntimeReadiness(
                worker_id=str(record.get("worker_id") or worker_id),
                backend=self.backend_name,
                readiness=readiness,
                reason="rmux-reference fixture readiness is telemetry only",
                evidence=self._fixture_evidence_from_record(record),
                warnings=["runtime readiness is not protocol terminal truth"],
            )
        return super().fetch_readiness(root, worker_id)

    def observe_status(self, root: Path, worker_id: str) -> dict[str, Any]:
        if self._has_rmux_fixture():
            record = self._resolve_fixture_record(worker_id)
            return {
                "backend": self.backend_name,
                "backend_type": "rmux",
                "worker_id": record.get("worker_id"),
                "session_id": record.get("session_id"),
                "window_id": record.get("window_id"),
                "pane_id": record.get("pane_id"),
                "process_id": record.get("process_id"),
                "status": record.get("readiness", "unknown"),
                "readiness": record.get("readiness", "unknown"),
                "evidence": [item.to_dict() for item in self._fixture_evidence_from_record(record)],
                "terminal_truth": "reports and markers remain owned by the upper protocol",
            }
        return super().observe_status(root, worker_id)

    def collect_runtime_metadata(self, root: Path, worker_id: str) -> dict[str, Any]:
        if self._has_rmux_fixture():
            record = self._resolve_fixture_record(worker_id)
            return {
                "backend": self.backend_name,
                "backend_type": "rmux",
                "worker_id": record.get("worker_id"),
                "fixture_dir": str(self.fixture_dir) if self.fixture_dir else "",
                "fixture_mode": True,
                "live_enabled": False,
                "rmux_default": True,
                "rmux_version": self._fixture_data().get("rmux_version", ""),
                "help_probe_summary": self._fixture_data().get("help_probe_summary", ""),
                "capabilities": [cap.to_dict() for cap in self._fixture_worker_from_record(record).capabilities],
            }
        return super().collect_runtime_metadata(root, worker_id)

    def submit_plan(self, root: Path, worker_id: str, task_reference: dict[str, Any] | None = None) -> SubmitPlan:
        if self._has_rmux_fixture():
            record = self._resolve_fixture_record(worker_id)
            binding = self._fixture_binding_from_record(record)
            ok = str(record.get("binding_confidence") or "") == "strong"
            blocked_reason = "" if ok else f"rmux worker {worker_id} binding_confidence is not strong: {record.get('binding_confidence') or '-'}"
            return SubmitPlan(
                worker_id=str(record.get("worker_id") or worker_id),
                backend_name=self.backend_name,
                backend_type="rmux",
                dispatch_allowed=ok,
                submit_supported=ok,
                submit_endpoint_kind="opencode_tui_api",
                target_host="127.0.0.1",
                target_port=None,
                target_pane=binding.pane_id,
                required_api_paths=["/tui/clear-prompt", "/tui/append-prompt", "/tui/submit-prompt"],
                planned_actions=["generate_submit_plan", "no_http_post", "no_marker_write"],
                blocked_reason=blocked_reason,
                warnings=list(binding.warnings),
                evidence=binding.evidence,
            )
        return super().submit_plan(root, worker_id, task_reference)

    def submit_capability(self) -> SubmitCapability:
        return SubmitCapability(
            supports_submit_plan=True,
            supports_live_submit=True,
            live_submit_requires_explicit_allow_real=True,
            allowed_endpoint_kinds=["opencode_tui_api"],
        )

    def _has_rmux_fixture(self) -> bool:
        return bool(self.fixture_dir and (self.fixture_dir / "rmux-workers.json").is_file())

    def _fixture_data(self) -> dict[str, Any]:
        if not self.fixture_dir:
            raise RuntimeAdapterError("rmux-reference fixture directory is required")
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

    def _fixture_records(self) -> list[dict[str, Any]]:
        workers = self._fixture_data().get("workers", [])
        if not isinstance(workers, list):
            raise RuntimeAdapterError("rmux fixture field 'workers' must be a list")
        return [self._normalize_fixture_record(item) for item in workers if isinstance(item, dict)]

    def _resolve_fixture_record(self, worker_id: str) -> dict[str, Any]:
        requested = str(worker_id or "").strip()
        matches = [record for record in self._fixture_records() if record.get("worker_id") == requested]
        if not matches:
            from .errors import WorkerNotFoundError

            raise WorkerNotFoundError(f"rmux worker not found: {requested}")
        identities = sorted({str(item.get("runtime_identity") or "") for item in matches})
        if len(matches) > 1 or len(identities) > 1:
            raise RuntimeAdapterError(f"ambiguous rmux worker: {requested}")
        return matches[0]

    def _normalize_fixture_record(self, record: dict[str, Any]) -> dict[str, Any]:
        worker = dict(record)
        worker["worker_id"] = str(worker.get("worker_id") or "")
        worker["runtime_identity"] = str(
            worker.get("runtime_identity")
            or f"{worker.get('session_id') or ''}:{worker.get('window_id') or ''}.{worker.get('pane_id') or ''}"
        )
        worker["role"] = str(worker.get("role") or "production_candidate")
        worker["allow_auto_submit"] = bool(worker.get("allow_auto_submit"))
        worker["default_for_new_tasks"] = bool(worker.get("default_for_new_tasks"))
        worker["session_id"] = str(worker.get("session_id") or "")
        worker["window_id"] = str(worker.get("window_id") or "")
        worker["pane_id"] = str(worker.get("pane_id") or worker.get("pane") or "")
        worker["process_id"] = int(worker.get("process_id") or 0) or None
        worker["readiness"] = str(worker.get("readiness") or "unknown")
        worker["binding_confidence"] = str(worker.get("binding_confidence") or "")
        capabilities = worker.get("capabilities")
        worker["capabilities"] = capabilities if isinstance(capabilities, list) else []
        evidence = worker.get("evidence")
        worker["evidence"] = evidence if isinstance(evidence, dict) else {}
        return worker

    def _fixture_worker_from_record(self, record: dict[str, Any]) -> RuntimeWorker:
        errors: list[str] = []
        if str(record.get("binding_confidence") or "") != "strong":
            errors.append(
                f"rmux worker {record.get('worker_id') or '-'} binding_confidence is not strong: {record.get('binding_confidence') or '-'}"
            )
        return RuntimeWorker(
            worker_id=str(record.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type="rmux",
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
            capabilities=[RuntimeCapability(name=str(item), supported=True) for item in record.get("capabilities", [])],
            evidence=self._fixture_evidence_from_record(record),
            dispatch_allowed=not errors,
            warnings=[],
            errors=errors,
            raw=record,
        )

    def _fixture_binding_from_record(self, record: dict[str, Any]) -> RuntimeBinding:
        warnings = [] if str(record.get("binding_confidence") or "") == "strong" else ["binding is not strong"]
        return RuntimeBinding(
            worker_id=str(record.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type="rmux",
            runtime_identity=str(record.get("runtime_identity") or ""),
            session_id=str(record.get("session_id") or ""),
            window_id=str(record.get("window_id") or ""),
            pane_id=str(record.get("pane_id") or ""),
            process_id=record.get("process_id"),
            binding_confidence=str(record.get("binding_confidence") or ""),
            evidence=self._fixture_evidence_from_record(record),
            warnings=warnings,
        )

    def _fixture_evidence_from_record(self, record: dict[str, Any]) -> list[RuntimeEvidence]:
        evidence = dict(record.get("evidence") or {})
        return [RuntimeEvidence("fixture", evidence)]

    def _worker_from_record(self, record: dict[str, Any]) -> RuntimeWorker:
        validation = validate_worker_for_dispatch(record, must_be_default=False)
        worker = validation["worker"]
        return RuntimeWorker(
            worker_id=str(worker.get("worker_id") or ""),
            backend=self.backend_name,
            backend_type="rmux",
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
            backend_type="rmux",
            pane_id=str(record.get("pane_id") or record.get("pane") or ""),
            port=record.get("port"),
            host=str(record.get("host") or ""),
            binding_confidence=str(record.get("binding_confidence") or ""),
            evidence=[RuntimeEvidence("registry", {"path": "opencode_visible_workers.json"})],
            warnings=[] if str(record.get("binding_confidence") or "") == "strong" else ["binding is not strong"],
        )

    def _live_probe_binding(self, binding: RuntimeBinding) -> dict[str, Any]:
        host = str(binding.host or "127.0.0.1" or "")
        port = binding.port
        if host not in {"127.0.0.1", "localhost"} or port is None:
            return {
                "host": host,
                "port": port,
                "health_ok": False,
                "doc_ok": False,
                "detected_tui_methods": [],
                "error": "host/port unavailable for localhost live probe",
            }
        health = self._http_get(host, int(port), "/global/health", max_bytes=64_000)
        doc = self._http_get(host, int(port), "/doc", max_bytes=512_000)
        doc_text = str(doc.get("body") or "")
        detected = [path for path in self.required_tui_paths if path in doc_text]
        return {
            "host": host,
            "port": int(port),
            "health_ok": bool(health.get("ok")),
            "doc_ok": bool(doc.get("ok")),
            "health_status": health.get("status"),
            "doc_status": doc.get("status"),
            "detected_tui_methods": detected,
            "all_required_tui_present": len(detected) == len(self.required_tui_paths),
        }

    def _live_pane_probe(self, binding: RuntimeBinding) -> dict[str, Any]:
        pane_id = str(binding.pane_id or "")
        result: dict[str, Any] = {
            "pane_id": pane_id,
            "pane_exists": False,
            "pane_pid": None,
            "pane_current_command": "",
            "pane_location": "",
            "opencode_process_present": False,
            "opencode_cmdline_contains_port": False,
            "descendant_pids": [],
        }
        if not pane_id:
            result["error"] = "binding has no pane_id"
            return result
        rmux_path = shutil.which("rmux") or "rmux"
        try:
            proc = subprocess.run(
                [
                    rmux_path,
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{session_name}:#{window_index}.#{pane_index}",
                ],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:
            result["error"] = str(exc)
            return result
        if proc.returncode != 0:
            result["error"] = proc.stderr.strip() or proc.stdout.strip() or "rmux list-panes failed"
            return result
        pane_line = ""
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if parts and parts[0] == pane_id:
                pane_line = line
                break
        if not pane_line:
            result["rmux_panes_stdout_preview"] = proc.stdout[:1000]
            return result
        parts = pane_line.split("\t")
        pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        result.update(
            {
                "pane_exists": True,
                "pane_pid": pid,
                "pane_current_command": parts[2] if len(parts) > 2 else "",
                "pane_location": parts[3] if len(parts) > 3 else "",
            }
        )
        tree_pids = [pid] if pid else []
        if pid:
            tree_pids.extend(self._descendant_pids(pid))
        result["descendant_pids"] = [item for item in tree_pids if item and item != pid]
        cmdlines = [self._proc_cmdline(item) for item in tree_pids if item]
        cmdlines = [item for item in cmdlines if item]
        text_blob = "\n".join(cmdlines).lower()
        result["opencode_process_present"] = bool(re.search(r"(?:^|\s|/)opencode(?:\s|$)", text_blob))
        port = binding.port
        if port is not None:
            result["opencode_cmdline_contains_port"] = any(
                f"--port {int(port)}" in item or f"--port={int(port)}" in item for item in cmdlines
            )
        return result

    def _http_get(self, host: str, port: int, path: str, *, max_bytes: int) -> dict[str, Any]:
        url = f"http://{host}:{int(port)}{path}"
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "omx-rmux-reference/1"})
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=2.0) as response:
                body = response.read(max_bytes + 1)
                truncated = len(body) > max_bytes
                if truncated:
                    body = body[:max_bytes]
                return {
                    "ok": 200 <= int(response.status) < 300,
                    "status": int(response.status),
                    "elapsed_seconds": round(time.time() - started, 3),
                    "body": body.decode("utf-8", errors="replace"),
                    "truncated": truncated,
                }
        except urllib.error.HTTPError as exc:
            return {"ok": False, "status": int(exc.code), "elapsed_seconds": round(time.time() - started, 3), "body": "", "truncated": False}
        except Exception as exc:
            return {"ok": False, "status": None, "elapsed_seconds": round(time.time() - started, 3), "body": str(exc), "truncated": False}

    def _proc_cmdline(self, pid: int) -> str:
        try:
            raw = (Path("/proc") / str(int(pid)) / "cmdline").read_bytes()
        except Exception:
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

    def _descendant_pids(self, pid: int) -> list[int]:
        seen: set[int] = set()
        queue = [int(pid)]
        descendants: list[int] = []
        while queue:
            current = queue.pop(0)
            try:
                proc = subprocess.run(
                    ["pgrep", "-P", str(current)],
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except Exception:
                continue
            if proc.returncode not in (0, 1):
                continue
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line.isdigit():
                    continue
                child = int(line)
                if child in seen:
                    continue
                seen.add(child)
                descendants.append(child)
                queue.append(child)
        return descendants
