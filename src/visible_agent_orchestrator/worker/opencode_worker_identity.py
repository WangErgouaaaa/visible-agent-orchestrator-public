"""No-live OpenCode visible worker identity diagnostics."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from visible_agent_orchestrator.runtime.registry import (
    validate_worker_for_dispatch,
)
from visible_agent_orchestrator.visible_registry_lock import (
    visible_binding_alias_conflicts,
)


SCHEMA_VERSION = 1
BACKEND_KIND = "opencode_tui"


def diagnose_opencode_worker_identity_from_files(
    *,
    root: Path | str,
    worker_id: str,
    opencode_visible_registry: Path | str,
) -> dict[str, Any]:
    root_path = Path(root).resolve()
    registry_path = Path(opencode_visible_registry).resolve()
    registry = _read_json(registry_path) if registry_path.exists() else {}
    record = dict(_find_worker(registry, worker_id))
    alias_conflicts = visible_binding_alias_conflicts(
        _list(registry.get("workers")),
        record,
        ignore_inactive_stale_aliases=True,
    )
    if alias_conflicts:
        record["binding_alias_quarantined"] = True
        record["binding_alias_conflicts"] = alias_conflicts
    tmux_panes = _tmux_panes(root_path)
    processes = _processes(root_path)
    listeners = _listeners(root_path)
    rebind = _rebind_check(root_path, worker_id)
    return diagnose_opencode_worker_identity(
        root=str(root_path),
        worker_id=worker_id,
        opencode_visible_registry=str(registry_path),
        registry_record=record,
        tmux_panes=tmux_panes,
        processes=processes,
        listeners=listeners,
        rebind_check=rebind,
    )


def diagnose_opencode_worker_identity_fixture(fixture_dir: Path | str) -> dict[str, Any]:
    fixture = Path(fixture_dir).resolve()
    payload = _read_json(fixture / "identity-input.json")
    return diagnose_opencode_worker_identity(
        root=str(payload.get("root", "/fixture/root")),
        worker_id=str(payload.get("worker_id", "vw-fixture4107")),
        opencode_visible_registry=str(payload.get("opencode_visible_registry", fixture / "opencode_visible_workers.json")),
        registry_record=_dict(payload.get("registry_record")),
        tmux_panes=_list(payload.get("tmux_panes")),
        processes=_list(payload.get("processes")),
        listeners=_list(payload.get("listeners")),
        rebind_check=_dict(payload.get("rebind_check")),
        resolved_worker_id=str(payload.get("resolved_worker_id", payload.get("worker_id", "vw-fixture4107"))),
        run_one_worker_scope_supported=bool(payload.get("run_one_worker_scope_supported", True)),
    )


def diagnose_opencode_worker_identity(
    *,
    root: str,
    worker_id: str,
    opencode_visible_registry: str,
    registry_record: dict[str, Any],
    tmux_panes: list[Any],
    processes: list[Any],
    listeners: list[Any],
    rebind_check: dict[str, Any] | None = None,
    resolved_worker_id: str = "",
    run_one_worker_scope_supported: bool = True,
) -> dict[str, Any]:
    record = _dict(registry_record)
    rebind = _dict(rebind_check)
    normalized_panes = [_normalize_pane(item) for item in tmux_panes]
    normalized_processes = [_normalize_process(item) for item in processes]
    normalized_listeners = [_normalize_listener(item) for item in listeners]

    registry_pane_id = str(record.get("pane_id", ""))
    registry_port = _int(record.get("port"))
    registry_backend = str(record.get("backend", record.get("backend_kind", "")))
    registry_binding = str(record.get("binding_confidence", ""))
    resolved = resolved_worker_id or worker_id
    pane = next((item for item in normalized_panes if str(item.get("pane_id", "")) == registry_pane_id), {})
    pane_pid = _int(pane.get("pane_pid") or record.get("pane_pid"))
    descendants = _descendant_pids(normalized_processes, pane_pid)
    pane_tree_pids = sorted({pane_pid, *descendants}) if pane_pid else []
    pane_processes = [item for item in normalized_processes if _int(item.get("pid")) in set(pane_tree_pids)]
    pane_contains_opencode = any(_looks_opencode(item) for item in pane_processes)

    port_listeners = [item for item in normalized_listeners if _int(item.get("port")) == registry_port and registry_port]
    listener_pids = sorted({_int(pid) for item in port_listeners for pid in _list(item.get("pids")) if _int(pid)})
    listener_processes = [item for item in normalized_processes if _int(item.get("pid")) in set(listener_pids)]
    listener_cmdlines = [str(item.get("args", item.get("cmdline", ""))) for item in listener_processes]
    port_live = bool(listener_pids)
    port_pid_maps_to_pane = bool(set(listener_pids).intersection(pane_tree_pids))
    ambiguous_candidate = len(listener_pids) > 1
    default_fallback_detected = bool(resolved and resolved != worker_id)

    submit_path_confidence = "strong" if pane_contains_opencode and port_pid_maps_to_pane and not ambiguous_candidate else "failed"
    rebind_confidence = str(rebind.get("binding_confidence", "")) or str(rebind.get("status", ""))
    divergence_detected = (
        registry_binding == "strong" and submit_path_confidence != "strong"
    ) or (
        rebind_confidence == "strong" and submit_path_confidence != "strong"
    ) or default_fallback_detected

    reasons: list[str] = []
    if not record:
        reasons.append("worker not found in OpenCode visible registry")
    if registry_backend != BACKEND_KIND:
        reasons.append(f"target worker backend must be {BACKEND_KIND}")
    if not run_one_worker_scope_supported:
        reasons.append("visible dispatch run-one does not support explicit worker scope")
    if default_fallback_detected:
        reasons.append(f"resolved worker {resolved} differs from requested worker {worker_id}")
    if not registry_pane_id:
        reasons.append("registry pane_id missing")
    if not pane:
        reasons.append(f"registry pane {registry_pane_id or '-'} not found")
    if not pane_contains_opencode:
        reasons.append(f"registry pane {registry_pane_id or '-'} process tree does not contain opencode")
    if not registry_port:
        reasons.append("registry port missing")
    if not port_live:
        reasons.append(f"port {registry_port or '-'} listener not found")
    if ambiguous_candidate:
        reasons.append(f"port {registry_port} has ambiguous listener pids: {listener_pids}")
    if listener_pids and not port_pid_maps_to_pane:
        reasons.append(f"port {registry_port} listener pid(s) {listener_pids} do not map to pane {registry_pane_id or '-'}")
    if registry_binding != "strong":
        reasons.append(f"registry binding is not strong: {registry_binding or '-'}")
    if divergence_detected:
        reasons.append("registry/rebind evidence diverges from submit-path identity evidence")

    dispatch_policy = validate_worker_for_dispatch(record)
    dispatch_policy_errors = [
        str(item)
        for item in dispatch_policy.get("errors", [])
    ]
    reasons.extend(dispatch_policy_errors)
    safe_for_live_submit = not reasons
    registry_stale = bool(record and not safe_for_live_submit and (not pane_contains_opencode or not port_pid_maps_to_pane))
    return {
        "schema_version": SCHEMA_VERSION,
        "diagnosis_kind": "opencode-worker-identity",
        "root": root,
        "worker_id": worker_id,
        "resolved_worker_id": resolved,
        "opencode_visible_registry": opencode_visible_registry,
        "registry_record_present": bool(record),
        "registry_pane_id": registry_pane_id,
        "registry_port": registry_port,
        "registry_backend_kind": registry_backend,
        "registry_binding_confidence": registry_binding,
        "registry_allow_auto_submit": bool(record.get("allow_auto_submit", False)) if record else False,
        "dispatch_policy_status": (
            "allowed"
            if dispatch_policy.get("dispatch_allowed")
            else "blocked"
        ),
        "dispatch_policy_errors": dispatch_policy_errors,
        "dispatch_policy_warnings": [
            str(item)
            for item in dispatch_policy.get("warnings", [])
        ],
        "registry_stale": registry_stale,
        "pane": pane,
        "pane_pid": pane_pid,
        "pane_process_tree_pids": pane_tree_pids,
        "pane_process_tree_contains_opencode": pane_contains_opencode,
        "pane_process_tree_summary": _process_summary(pane_processes),
        "port_live": port_live,
        "listener_pid": listener_pids[0] if len(listener_pids) == 1 else None,
        "listener_pids": listener_pids,
        "listener_cmdline": listener_cmdlines[0] if len(listener_cmdlines) == 1 else "",
        "listener_cmdlines": listener_cmdlines,
        "port_pid_maps_to_pane": port_pid_maps_to_pane,
        "ambiguous_candidate": ambiguous_candidate,
        "run_one_worker_scope_supported": run_one_worker_scope_supported,
        "default_worker_fallback_detected": default_fallback_detected,
        "rebind_check_confidence": rebind_confidence,
        "rebind_check_status": str(rebind.get("status", "")),
        "submit_path_confidence": submit_path_confidence,
        "divergence_detected": divergence_detected,
        "safe_for_live_submit": safe_for_live_submit,
        "rebind_required": registry_stale,
        "registry_stale_or_dispatch_surface_unsupported": bool(registry_stale or not run_one_worker_scope_supported),
        "reasons": _dedupe(reasons),
        "no_live_actions_performed": True,
        "no_registry_writes": True,
    }


def _tmux_panes(root: Path) -> list[dict[str, Any]]:
    mux = _current_substrate()
    if not mux:
        return []
    result = _run([mux, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}"], root)
    panes: list[dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            panes.append({"pane_id": parts[0], "pane_pid": _int(parts[1]), "current_command": parts[2], "current_path": parts[3], "location": parts[4]})
    return panes


def _current_substrate() -> str:
    if os.environ.get("RMUX") or str(os.environ.get("TERM_PROGRAM", "")).lower() == "rmux":
        return "rmux"
    if os.environ.get("TMUX"):
        return "tmux"
    return ""


def _processes(root: Path) -> list[dict[str, Any]]:
    result = _run(["ps", "-eo", "pid,ppid,pgid,stat,comm,args"], root)
    rows: list[dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines()[1:]:
        parts = line.strip().split(None, 5)
        if len(parts) >= 6:
            rows.append({"pid": _int(parts[0]), "ppid": _int(parts[1]), "pgid": _int(parts[2]), "stat": parts[3], "comm": parts[4], "args": parts[5]})
    return rows


def _listeners(root: Path) -> list[dict[str, Any]]:
    result = _run(["ss", "-ltnp"], root)
    if result.get("returncode") != 0:
        result = _run(["netstat", "-ltnp"], root)
    rows: list[dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines():
        ports = [int(item) for item in re.findall(r":(\d+)\b", line)]
        pids = [int(item) for item in re.findall(r"pid=(\d+)", line)]
        if not pids:
            # netstat format: 1234/program
            pids = [int(item) for item in re.findall(r"\s(\d+)/", line)]
        for port in ports[-1:]:
            rows.append({"port": port, "pids": pids, "line": line})
    return rows


def _rebind_check(root: Path, worker_id: str) -> dict[str, Any]:
    cmd = [str(root / "bin" / "omx_visible_rebind"), "check", "--root", str(root), "--worker", worker_id, "--json"]
    result = _run(cmd, root)
    try:
        return json.loads(result.get("stdout", ""))
    except json.JSONDecodeError:
        return {"status": "", "binding_confidence": "", "command_result": result}


def _run(argv: list[str], cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=30, check=False)
        return {"argv": argv, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "returncode": 1, "stdout": "", "stderr": str(exc)}


def _find_worker(registry: dict[str, Any], worker_id: str) -> dict[str, Any]:
    for item in _list(registry.get("workers")):
        if isinstance(item, dict) and str(item.get("worker_id", "")) == worker_id:
            return item
    return {}


def _normalize_pane(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    text = str(item)
    match = re.search(r"(?P<loc>\S+)\s+(?P<pane>%\d+)\s+pid=(?P<pid>\d+)\s+command=(?P<cmd>\S+)\s+path=(?P<path>.*)", text)
    if not match:
        return {}
    return {"location": match.group("loc"), "pane_id": match.group("pane"), "pane_pid": _int(match.group("pid")), "current_command": match.group("cmd"), "current_path": match.group("path")}


def _normalize_process(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    parts = str(item).strip().split(None, 5)
    if len(parts) >= 6 and parts[0].isdigit():
        return {"pid": _int(parts[0]), "ppid": _int(parts[1]), "pgid": _int(parts[2]), "stat": parts[3], "comm": parts[4], "args": parts[5]}
    return {}


def _normalize_listener(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    text = str(item)
    ports = [int(value) for value in re.findall(r":(\d+)\b", text)]
    pids = [int(value) for value in re.findall(r"pid=(\d+)", text)]
    return {"port": ports[-1] if ports else 0, "pids": pids, "line": text}


def _descendant_pids(processes: list[dict[str, Any]], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for item in processes:
        children.setdefault(_int(item.get("ppid")), []).append(_int(item.get("pid")))
    seen: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _looks_opencode(process: dict[str, Any]) -> bool:
    comm = Path(str(process.get("comm", "")).split()[0]).name.lower()
    args = str(process.get("args", "")).strip()
    argv0 = args.split(None, 1)[0] if args else ""
    argv0_base = Path(argv0).name.lower()
    return comm == "opencode" or argv0_base == "opencode"


def _process_summary(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"pid": _int(item.get("pid")), "ppid": _int(item.get("ppid")), "comm": str(item.get("comm", "")), "args": str(item.get("args", ""))[:240]} for item in processes]


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
