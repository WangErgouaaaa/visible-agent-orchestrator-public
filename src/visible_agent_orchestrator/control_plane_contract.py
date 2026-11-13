"""Normalized control-plane contract over the visible-dispatch CLI surfaces.

This layer keeps LangGraph and other control-plane callers on a stable action
schema while the underlying execution plane remains the existing
`omx_visible_dispatch` / `omx_visible_health` / `omx_visible_rebind` toolchain.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from visible_agent_orchestrator.prompt_budget import build_prompt_budget, prompt_budget_error


CONTRACT_VERSION = 1
TERMINAL_RECONCILE_WAIT_SECONDS = 120
CommandRunner = Callable[[list[str], Path, int], dict[str, Any]]
CONTROL_BODY_SECTION_HEADERS = {
    "Task:",
    "Report:",
    "Semantic Review:",
    "Report Contract:",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_command_runner(argv: list[str], cwd: Path, timeout: int = 300) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "argv": argv,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
            "started_at": started.isoformat(),
            "finished_at": utc_now(),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "command timed out",
            "timed_out": True,
            "started_at": started.isoformat(),
            "finished_at": utc_now(),
        }


def _control_preamble(text: str) -> str:
    lines: list[str] = []
    for line in str(text or "").splitlines():
        if line.strip() in CONTROL_BODY_SECTION_HEADERS:
            break
        lines.append(line)
    return "\n".join(lines)


def parse_key_value_lines(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in _control_preamble(text).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or key.startswith(("{", "[", "- ")):
            continue
        parsed[key] = value.strip()
    return parsed


def extract_report_excerpt(text: str) -> str:
    marker = "\nReport:\n"
    raw = str(text or "")
    if marker not in raw:
        return ""
    return raw.split(marker, 1)[1].strip()


def _warning_matches_post_terminal_busy(worker_id: str, warning: str) -> bool:
    warning_text = str(warning or "").strip()
    return warning_text == "post_terminal_tui_busy" or warning_text == f"{worker_id} post_terminal_tui_busy"


def _continuation_closeout_note(worker_id: str, continuation_state: str) -> str:
    return f"{worker_id} post_terminal_continuation_state={continuation_state}"


def _classify_provider_error(actions: list[dict[str, Any]]) -> dict[str, Any]:
    for action in actions:
        parsed = action.get("parsed")
        if isinstance(parsed, dict) and str(parsed.get("provider_error") or "").strip() == "provider_overloaded":
            return {
                "detected": True,
                "kind": "provider_overloaded",
                "retryable": True,
            }
    return {
        "detected": False,
        "kind": "",
        "retryable": False,
    }


@dataclass
class VisibleDispatchContract:
    root: Path
    backend: str = "tmux-reference"
    runner: CommandRunner = default_command_runner
    max_prompt_bytes: int | None = None
    model: str = ""
    nominal_context_tokens: int | None = None
    effective_context_tokens: int | None = None

    def _run(
        self,
        action: str,
        argv: list[str],
        timeout: int,
        *,
        worker_id: str = "",
        state: str = "",
        terminal: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.runner(argv, self.root, timeout)
        parsed = parse_key_value_lines(result.get("stdout", ""))
        returncode = int(result.get("returncode", 1))
        ok = returncode == 0 and not bool(result.get("timed_out"))
        payload = {
            "contract_version": CONTRACT_VERSION,
            "contract_action": action,
            "worker_id": worker_id,
            "backend": self.backend,
            "argv": result.get("argv", argv),
            "returncode": returncode,
            "timed_out": bool(result.get("timed_out")),
            "ok": ok,
            "state": state,
            "terminal": bool(terminal),
            "started_at": result.get("started_at", ""),
            "finished_at": result.get("finished_at", ""),
            "stdout": str(result.get("stdout") or ""),
            "stderr": str(result.get("stderr") or ""),
            "parsed": parsed,
            "warnings": [],
            "errors": [],
        }
        if not ok:
            payload["errors"].append(f"{action} failed")
        if extra:
            payload.update(extra)
        return payload

    def doctor(self, worker_id: str) -> dict[str, Any]:
        return self._run(
            "doctor",
            [
                str(self.root / "bin" / "omx_visible_dispatch"),
                "doctor",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="ok",
        )

    def health(self, worker_id: str) -> dict[str, Any]:
        return self._run(
            "health",
            [
                str(self.root / "bin" / "omx_visible_health"),
                "check",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="ok",
        )

    def locks(self, worker_id: str) -> dict[str, Any]:
        return self._run(
            "locks",
            [
                str(self.root / "bin" / "omx_visible_dispatch"),
                "locks",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
            ],
            120,
            worker_id=worker_id,
            state="ok",
        )

    def rebind_check(self, worker_id: str) -> dict[str, Any]:
        return self._run(
            "rebind_check",
            [
                str(self.root / "bin" / "omx_visible_rebind"),
                "check",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="ok",
        )

    def preflight(self, workers: list[str]) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []
        for worker_id in workers:
            for item in (self.doctor(worker_id), self.health(worker_id), self.rebind_check(worker_id)):
                checks.append(item)
                if not item["ok"]:
                    errors.append(f"{worker_id} {item['contract_action']} failed")
        return {
            "contract_version": CONTRACT_VERSION,
            "checked_at": utc_now(),
            "checks": checks,
            "errors": list(dict.fromkeys(errors)),
            "warnings": list(dict.fromkeys(warnings)),
        }

    def submit(
        self,
        worker_id: str,
        title: str,
        body: str,
        *,
        allow_real: bool,
        task_contract: dict[str, Any] | None = None,
        session_action: str = "",
        session_action_reason: str = "",
        rotation_reason: str = "",
        rehydration_packet_type: str = "",
    ) -> dict[str, Any]:
        if not allow_real:
            return {
                "contract_version": CONTRACT_VERSION,
                "contract_action": "submit",
                "worker_id": worker_id,
                "backend": self.backend,
                "argv": [],
                "returncode": 1,
                "timed_out": False,
                "ok": False,
                "state": "submit_rejected_missing_allow_real",
                "terminal": True,
                "started_at": "",
                "finished_at": utc_now(),
                "stdout": "",
                "stderr": "ERROR: submit requires --allow-real\n",
                "parsed": {},
                "warnings": [],
                "errors": ["submit requires --allow-real"],
                "title": title,
                "run_id": "",
                "task_id": "",
                "created_task": "",
                "closeout_notes": ["submit_rejected_missing_allow_real_before_submit_new"],
            }
        budget = build_prompt_budget(
            title=title,
            body=body,
            model=self.model,
            worker_id=worker_id,
            max_prompt_bytes=self.max_prompt_bytes,
            nominal_context_tokens=self.nominal_context_tokens,
            effective_context_tokens=self.effective_context_tokens,
        )
        if budget["status"] != "ok":
            return {
                "contract_version": CONTRACT_VERSION,
                "contract_action": "submit",
                "worker_id": worker_id,
                "backend": self.backend,
                "argv": [],
                "returncode": 1,
                "timed_out": False,
                "ok": False,
                "state": "prompt_budget_rejected",
                "terminal": False,
                "started_at": "",
                "finished_at": utc_now(),
                "stdout": "",
                "stderr": "",
                "parsed": {},
                "warnings": [],
                "errors": [prompt_budget_error(worker_id, budget)],
                "title": title,
                "run_id": "",
                "task_id": "",
                "created_task": "",
                "prompt_budget": budget,
                "closeout_notes": ["prompt_budget_rejected_before_submit"],
            }
        normalized_task_contract: dict[str, Any] | None = None
        task_contract_json = ""
        if task_contract is not None:
            try:
                normalized_task_contract = dict(task_contract)
                task_contract_json = json.dumps(
                    normalized_task_contract,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            except (TypeError, ValueError) as exc:
                return {
                    "contract_version": CONTRACT_VERSION,
                    "contract_action": "submit",
                    "worker_id": worker_id,
                    "backend": self.backend,
                    "argv": [],
                    "returncode": 1,
                    "timed_out": False,
                    "ok": False,
                    "state": "submit_contract_invalid",
                    "terminal": False,
                    "started_at": "",
                    "finished_at": utc_now(),
                    "stdout": "",
                    "stderr": "",
                    "parsed": {},
                    "warnings": [],
                    "errors": [f"{worker_id} task contract is not JSON serializable: {exc}"],
                    "title": title,
                    "run_id": "",
                    "task_id": "",
                    "created_task": "",
                    "prompt_budget": budget,
                    "closeout_notes": ["task_contract_rejected_before_submit"],
                }
        argv = [
            str(self.root / "bin" / "omx_visible_dispatch"),
            "submit-new",
            "--root",
            str(self.root),
            "--backend",
            self.backend,
            "--worker",
            worker_id,
            "--title",
            title,
            "--body",
            body,
        ]
        if normalized_task_contract is not None:
            argv.extend(["--task-contract-json", task_contract_json])
        if session_action:
            argv.extend(
                [
                    "--session-action",
                    session_action,
                    "--session-action-reason",
                    session_action_reason,
                ]
            )
        if session_action == "rotate":
            argv.extend(
                [
                    "--rotation-reason",
                    rotation_reason,
                    "--rehydration-packet-type",
                    rehydration_packet_type,
                ]
            )
        if allow_real:
            argv.append("--allow-real")
        action = self._run("submit", argv, 180, worker_id=worker_id, state="submitted")
        parsed = action["parsed"]
        action.update(
            {
                "title": title,
                "run_id": str(parsed.get("run_id") or ""),
                "task_id": str(parsed.get("task_id") or ""),
                "created_task": str(parsed.get("created_task") or ""),
            }
        )
        if not action["ok"]:
            action["state"] = "submit_failed"
            action["errors"] = [f"{worker_id} submit failed"]
        elif normalized_task_contract is not None:
            action["task_contract"] = normalized_task_contract
        return action

    def execute_command(self, worker_id: str, command_body: str, *, allow_real: bool) -> dict[str, Any]:
        if not allow_real:
            return {
                "contract_version": CONTRACT_VERSION,
                "contract_action": "execute_command",
                "worker_id": worker_id,
                "backend": self.backend,
                "argv": [],
                "returncode": 1,
                "timed_out": False,
                "ok": False,
                "state": "execute_command_rejected_missing_allow_real",
                "terminal": True,
                "started_at": "",
                "finished_at": utc_now(),
                "stdout": "",
                "stderr": "ERROR: execute-command requires --allow-real\n",
                "parsed": {},
                "warnings": [],
                "errors": ["execute-command requires --allow-real"],
                "command_body": command_body,
                "closeout_notes": ["execute_command_rejected_missing_allow_real"],
            }
        argv = [
            str(self.root / "bin" / "omx_opencode_tui_adapter"),
            "execute-command-real",
            "--root",
            str(self.root),
            "--worker",
            worker_id,
            "--runtime-backend",
            self.backend,
            "--command",
            command_body,
            "--allow-real",
        ]
        action = self._run("execute_command", argv, 90, worker_id=worker_id, state="executed")
        parsed = action["parsed"]
        action.update(
            {
                "command_body": command_body,
                "reset_session_id": str(parsed.get("reset_session_id") or ""),
                "reset_epoch": str(parsed.get("reset_epoch") or ""),
                "checked_at": str(parsed.get("checked_at") or ""),
                "evidence_source": str(parsed.get("evidence_source") or parsed.get("reset_evidence_source") or ""),
            }
        )
        if not action["ok"]:
            action["errors"] = [f"{worker_id} execute-command failed"]
        return action

    def session_list(self, worker_id: str) -> dict[str, Any]:
        return self._run(
            "session_list",
            [
                str(self.root / "bin" / "omx_opencode_tui_adapter"),
                "session-list",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="snapshot",
        )

    def session_status(self, worker_id: str) -> dict[str, Any]:
        return self._run(
            "session_status",
            [
                str(self.root / "bin" / "omx_opencode_tui_adapter"),
                "session-status",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="snapshot",
        )

    def session_messages(self, worker_id: str, session_id: str) -> dict[str, Any]:
        return self._run(
            "session_messages",
            [
                str(self.root / "bin" / "omx_opencode_tui_adapter"),
                "session-messages",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--session-id",
                session_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="snapshot",
        )

    def capabilities(self, worker_id: str) -> dict[str, Any]:
        action = self._run(
            "capabilities",
            [
                str(self.root / "bin" / "omx_opencode_tui_adapter"),
                "experimental-capabilities",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="snapshot",
        )
        parsed = action["parsed"]
        action.update(
            {
                "background_subagents": str(parsed.get("backgroundSubagents") or ""),
                "checked_at": str(parsed.get("checked_at") or ""),
            }
        )
        if not action["ok"]:
            action["errors"] = [f"{worker_id} capabilities failed"]
        return action

    def background_output(self, worker_id: str, session_id: str, task_id: str) -> dict[str, Any]:
        action = self._run(
            "background_output",
            [
                str(self.root / "bin" / "omx_opencode_tui_adapter"),
                "background-output-real",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--runtime-backend",
                self.backend,
            ],
            120,
            worker_id=worker_id,
            state="snapshot",
        )
        parsed = action["parsed"]
        action.update(
            {
                "session_id": session_id,
                "task_id": task_id,
                "state_name": str(parsed.get("state") or ""),
                "raw_status": str(parsed.get("raw_status") or ""),
                "completion_seen": bool(parsed.get("completion_seen") is True),
                "output_present": bool(parsed.get("output_present") is True),
                "output_chars": int(parsed.get("output_chars") or 0),
                "retrieval_ready": str(parsed.get("retrieval_ready") or ""),
                "canonical_retrieval_handle": str(parsed.get("canonical_retrieval_handle") or ""),
                "fallback_session_id": str(parsed.get("fallback_session_id") or ""),
                "retry_after_ms": str(parsed.get("retry_after_ms") or ""),
                "checked_at": str(parsed.get("checked_at") or ""),
            }
        )
        if not action["ok"]:
            action["errors"] = [f"{worker_id} background_output failed"]
        return action

    def background_control(
        self,
        worker_id: str,
        session_id: str,
        task_id: str,
        request_body: dict[str, Any],
        *,
        allow_real: bool,
    ) -> dict[str, Any]:
        if not allow_real:
            return {
                "contract_version": CONTRACT_VERSION,
                "contract_action": "background_control",
                "worker_id": worker_id,
                "backend": self.backend,
                "argv": [],
                "returncode": 1,
                "timed_out": False,
                "ok": False,
                "state": "background_control_rejected_missing_allow_real",
                "terminal": True,
                "started_at": "",
                "finished_at": utc_now(),
                "stdout": "",
                "stderr": "ERROR: background-control requires --allow-real\n",
                "parsed": {},
                "warnings": [],
                "errors": ["background-control requires --allow-real"],
                "closeout_notes": ["background_control_rejected_missing_allow_real"],
            }
        request_body_json = json.dumps(dict(request_body), separators=(",", ":"), ensure_ascii=False)
        action = self._run(
            "background_control",
            [
                str(self.root / "bin" / "omx_opencode_tui_adapter"),
                "background-control-real",
                "--root",
                str(self.root),
                "--worker",
                worker_id,
                "--session-id",
                session_id,
                "--task-id",
                task_id,
                "--request-body-json",
                request_body_json,
                "--runtime-backend",
                self.backend,
                "--allow-real",
            ],
            120,
            worker_id=worker_id,
            state="executed",
        )
        parsed = action["parsed"]
        action.update(
            {
                "session_id": session_id,
                "task_id": task_id,
                "request_body": dict(request_body),
                "action_requested": str(parsed.get("action_requested") or ""),
                "action_applied": str(parsed.get("action_applied") or ""),
                "state_name": str(parsed.get("state") or ""),
                "required_or_optional": str(parsed.get("required_or_optional") or ""),
                "actuation_gate": str(parsed.get("actuation_gate") or ""),
                "canonical_retrieval_handle": str(parsed.get("canonical_retrieval_handle") or ""),
                "checked_at": str(parsed.get("checked_at") or ""),
            }
        )
        if not action["ok"]:
            action["errors"] = [f"{worker_id} background_control failed"]
        return action

    def wait(self, worker_id: str, run_id: str, task_id: str, *, timeout: int) -> dict[str, Any]:
        action = self._run(
            "wait",
            [
                str(self.root / "bin" / "omx_visible_dispatch"),
                "wait",
                "--root",
                str(self.root),
                "--backend",
                self.backend,
                "--run",
                run_id,
                "--task",
                task_id,
                "--timeout",
                str(timeout),
            ],
            timeout + 120,
            worker_id=worker_id,
            state="waiting",
        )
        action["wait_result"] = str(action["parsed"].get("wait_result") or "")
        action["state"] = action["wait_result"] or action["state"]
        if str(action["parsed"].get("post_terminal_tui_readiness") or "") == "busy":
            action["warnings"] = [f"{worker_id} post_terminal_tui_busy"]
        action["post_terminal_continuation_state"] = str(action["parsed"].get("post_terminal_continuation_state") or "")
        if not action["ok"]:
            action["errors"] = [f"{worker_id} wait failed"]
        return action

    def collect(self, worker_id: str, run_id: str, task_id: str) -> dict[str, Any]:
        action = self._run(
            "collect",
            [
                str(self.root / "bin" / "omx_visible_dispatch"),
                "collect",
                "--root",
                str(self.root),
                "--backend",
                self.backend,
                "--run",
                run_id,
                "--task",
                task_id,
                "--compact",
            ],
            180,
            worker_id=worker_id,
            state="collecting",
        )
        action["collect_status"] = str(action["parsed"].get("status") or "")
        action["report_excerpt"] = extract_report_excerpt(str(action.get("stdout", "")))
        action["report_present_authoritative"] = "report_present" in action["parsed"]
        action["report_non_empty_authoritative"] = "report_non_empty" in action["parsed"]
        action["terminal_markers"] = [
            marker.strip()
            for marker in str(action["parsed"].get("terminal_markers") or "").split(",")
            if marker.strip() and marker.strip() != "-"
        ]
        action["report_present"] = str(action["parsed"].get("report_present") or "").strip().lower() == "true"
        action["report_non_empty"] = str(action["parsed"].get("report_non_empty") or "").strip().lower() == "true"
        action["terminal_marker_observed_at"] = str(action.get("finished_at") or "")
        action["state"] = action["collect_status"] or action["state"]
        action["post_terminal_continuation_state"] = str(action["parsed"].get("post_terminal_continuation_state") or "")
        terminal_collect_states = {
            "completed",
            "failed",
            "blocked",
            "invalid_terminal_no_report",
            "invalid_conflicting_terminal_markers",
        }
        if action["collect_status"] in terminal_collect_states:
            action["ok"] = True
            action["errors"] = []
        if not action["ok"]:
            action["errors"] = [f"{worker_id} collect failed"]
        return action

    def settle(self, worker_id: str, run_id: str, task_id: str) -> dict[str, Any]:
        action = self._run(
            "settle",
            [
                str(self.root / "bin" / "omx_visible_dispatch"),
                "settle",
                "--root",
                str(self.root),
                "--backend",
                self.backend,
                "--run",
                run_id,
                "--task",
                task_id,
                "--allow-terminal-archive",
            ],
            180,
            worker_id=worker_id,
            state="settled",
            terminal=True,
        )
        action["lock_state_after"] = str(action["parsed"].get("lock_state_after") or "")
        if not action["ok"]:
            action["errors"] = [f"{worker_id} settle failed"]
        return action

    def recover(self, worker_id: str) -> dict[str, Any]:
        action = self._run(
            "recover",
            [
                str(self.root / "bin" / "omx_visible_dispatch"),
                "settle-latest",
                "--root",
                str(self.root),
                "--backend",
                self.backend,
                "--worker",
                worker_id,
                "--allow-terminal-archive",
            ],
            180,
            worker_id=worker_id,
            state="recovered",
            terminal=True,
        )
        action.update(
            {
                "latest_run_id": str(action["parsed"].get("latest_run_id") or ""),
                "latest_task_id": str(action["parsed"].get("latest_task_id") or ""),
                "next_safe_action": str(action["parsed"].get("next_safe_action") or ""),
                "lock_state_after": str(action["parsed"].get("lock_state_after") or ""),
            }
        )
        if not action["ok"]:
            action["errors"] = [f"{worker_id} recover failed"]
        return action

    def execute_submission(self, submission: dict[str, Any], *, wait_timeout: int) -> dict[str, Any]:
        worker_id = str(submission.get("worker_id") or "")
        if submission.get("dry_run"):
            return {
                "contract_version": CONTRACT_VERSION,
                "contract_action": "execute_submission",
                "worker_id": worker_id,
                "backend": self.backend,
                "run_id": "",
                "task_id": "",
                "state": "dry_run",
                "wait_result": "dry_run",
                "collect_status": "dry_run",
                "settle_returncode": 0,
                "actions": [],
                "warnings": [],
                "errors": [],
                "closeout_notes": [],
                "post_terminal_busy_recovered": False,
            }
        if int(submission.get("returncode", 1)) != 0 or submission.get("ok") is False:
            state = str(submission.get("state") or "submit_failed")
            errors = list(submission.get("errors", [])) or [f"{worker_id} submit failed"]
            return {
                "contract_version": CONTRACT_VERSION,
                "contract_action": "execute_submission",
                "worker_id": worker_id,
                "backend": self.backend,
                "run_id": str(submission.get("run_id") or ""),
                "task_id": str(submission.get("task_id") or ""),
                "state": state,
                "submission_failed": True,
                "wait_result": state,
                "collect_status": state,
                "settle_returncode": 1,
                "actions": [],
                "warnings": [],
                "errors": errors,
                "closeout_notes": list(submission.get("closeout_notes", [])),
                "post_terminal_busy_recovered": False,
                "prompt_budget": submission.get("prompt_budget", {}),
            }
        run_id = str(submission.get("run_id") or "")
        task_id = str(submission.get("task_id") or "")
        wait_action = self.wait(worker_id, run_id, task_id, timeout=wait_timeout)
        collect_action = self.collect(worker_id, run_id, task_id)
        actions = [wait_action, collect_action]
        initial_wait_state = str(wait_action.get("wait_result") or "")
        initial_collect_state = str(collect_action.get("collect_status") or "")
        terminal_reconciliation_attempted = False
        wait_state = str(wait_action.get("wait_result") or "")
        collect_state = str(collect_action.get("collect_status") or "")
        terminal_collect_states = {
            "completed",
            "failed",
            "blocked",
            "invalid_terminal_no_report",
            "invalid_conflicting_terminal_markers",
        }
        if (
            initial_wait_state == "running_or_incomplete"
            and initial_collect_state == "running_or_incomplete"
            and not _classify_provider_error(actions)["detected"]
        ):
            terminal_reconciliation_attempted = True
            wait_action = self.wait(
                worker_id,
                run_id,
                task_id,
                timeout=max(1, min(wait_timeout, TERMINAL_RECONCILE_WAIT_SECONDS)),
            )
            collect_action = self.collect(worker_id, run_id, task_id)
            actions.extend([wait_action, collect_action])
            wait_state = str(wait_action.get("wait_result") or "")
            collect_state = str(collect_action.get("collect_status") or "")
        settle_action: dict[str, Any] | None = None
        if collect_state in terminal_collect_states:
            settle_action = self.settle(worker_id, run_id, task_id)
            actions.append(settle_action)
        state = (
            collect_state
            if collect_state in terminal_collect_states and wait_state != "failed"
            else (wait_state or collect_state or "unknown")
        )
        errors = list(submission.get("errors", []))
        warnings = list(submission.get("warnings", []))
        if not (
            terminal_reconciliation_attempted
            and collect_state in terminal_collect_states
        ):
            errors.extend(wait_action.get("errors", []))
            errors.extend(collect_action.get("errors", []))
        if settle_action is not None:
            errors.extend(settle_action.get("errors", []))
        warnings.extend(wait_action.get("warnings", []))
        warnings.extend(collect_action.get("warnings", []))
        if settle_action is not None:
            warnings.extend(settle_action.get("warnings", []))
        closeout_notes = list(submission.get("closeout_notes", []))
        post_terminal_continuation_state = str(
            collect_action.get("post_terminal_continuation_state")
            or wait_action.get("post_terminal_continuation_state")
            or ""
        )
        post_terminal_busy_recovered = False
        if (
            state == "completed"
            and settle_action is not None
            and int(settle_action.get("returncode", 1)) == 0
            and str(settle_action.get("lock_state_after") or "") == "none"
        ):
            retained_warnings: list[str] = []
            for item in warnings:
                if _warning_matches_post_terminal_busy(worker_id, str(item)):
                    post_terminal_busy_recovered = True
                    continue
                retained_warnings.append(str(item))
            warnings = retained_warnings
            if post_terminal_busy_recovered:
                closeout_notes.append(f"{worker_id} post_terminal_tui_busy_recovered")
        if state == "completed" and post_terminal_continuation_state:
            closeout_notes.append(_continuation_closeout_note(worker_id, post_terminal_continuation_state))
        if (
            terminal_reconciliation_attempted
            and collect_state in terminal_collect_states
        ):
            closeout_notes.append(f"{worker_id} terminal_reconciled_after_initial_incomplete")
        provider_error = _classify_provider_error(actions)
        if provider_error["detected"]:
            note = f"{worker_id} provider_retryable_error={provider_error['kind']}"
            errors.append(note)
            closeout_notes.append(note)
        if state not in {"completed", "blocked", "failed"}:
            errors.append(f"{worker_id} wait/collect ended in {state}")
        return {
            "contract_version": CONTRACT_VERSION,
            "contract_action": "execute_submission",
            "worker_id": worker_id,
            "backend": self.backend,
            "run_id": run_id,
            "task_id": task_id,
            "state": state,
            "wait_result": str(wait_action.get("wait_result") or ""),
            "collect_status": str(collect_action.get("collect_status") or ""),
            "settle_returncode": (
                int(settle_action.get("returncode", 1))
                if settle_action is not None
                else None
            ),
            "report_excerpt": str(collect_action.get("report_excerpt") or ""),
            "report_present": (
                bool(collect_action.get("report_present"))
                if collect_action.get("report_present_authoritative")
                else bool(collect_action.get("report_excerpt"))
            ),
            "report_non_empty": (
                bool(collect_action.get("report_non_empty"))
                if collect_action.get("report_non_empty_authoritative")
                else bool(collect_action.get("report_excerpt"))
            ),
            "terminal_markers": list(collect_action.get("terminal_markers", [])),
            "terminal_marker_observed_at": str(collect_action.get("terminal_marker_observed_at") or ""),
            "late_terminal_marker_observed": bool(
                (
                    terminal_reconciliation_attempted
                    and collect_state in terminal_collect_states
                )
                or (
                    collect_state in terminal_collect_states
                    and wait_state
                    and wait_state not in {collect_state, "failed"}
                )
            ),
            "actions": actions,
            "warnings": list(dict.fromkeys(warnings)),
            "errors": list(dict.fromkeys(errors)),
            "closeout_notes": list(dict.fromkeys(closeout_notes)),
            "terminal_reconciliation_attempted": terminal_reconciliation_attempted,
            "post_terminal_busy_recovered": post_terminal_busy_recovered,
            "post_terminal_continuation_state": post_terminal_continuation_state,
            "provider_error": provider_error,
        }
