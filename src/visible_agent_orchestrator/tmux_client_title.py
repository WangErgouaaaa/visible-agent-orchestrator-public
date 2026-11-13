"""Standalone tmux client-title product.

Preserves the tmux session-title / VSCode terminal-tab title capability that
previously lived inside the retired team-board sidebar. This module is
board-independent: it derives titles from live tmux pane metadata only.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TMUX_SESSION_TITLE_LABELS = {
    "running": "🟡 开发中",
    "idle": "🔵 空闲",
    "done": "✅ 已完成",
    "failed": "❌ 失败",
    "blocked": "🟠 阻塞",
    "unknown": "⚪ 未知",
}
TMUX_AGENT_COMMANDS = {"claude", "codex", "node", "opencode"}
TMUX_AGENT_TITLE_WORDS = ("claude", "codex", "opencode")
TMUX_RUNNING_TITLE_WORDS = ("处理中", "执行中", "开发中", "incomplete tasks", "system directive", "read-only")
TMUX_IDLE_TITLE_WORDS = ("| 会话", "会话")
TMUX_DONE_TITLE_MARKER = re.compile(r"^(?:✅\s*)?(?:agent完成|完成)(?:$|\s)", re.I)
TMUX_BLOCKED_TITLE_MARKER = re.compile(r"^(?:⛔\s*)?(?:agent阻塞|阻塞)(?:$|\s)", re.I)
TMUX_FAILED_TITLE_MARKER = re.compile(r"^(?:❌\s*)?(?:agent失败|失败)(?:$|\s)", re.I)
TMUX_SPINNER_PREFIXES = tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
TMUX_PANE_METADATA_FORMAT = (
    "#{session_name}\t#{pane_id}\t#{history_bytes}\t"
    "#{pane_current_command}\t#{pane_title}\t#{pane_dead}"
)
TMUX_CAPTURE_TAIL_LINES = 120
TMUX_CONTENT_STATE_TAIL_LINES = 40
TMUX_RUNNING_CONTENT_PATTERN = re.compile(
    r"\bPursuing goal\b|\bWaiting for background terminal\b|"
    r"^\s*[•▸›-]*\s*Working\s*\(|\bWriting command\b|\besc interrupt\b",
    re.I | re.M,
)
TMUX_PROTOCOL_TERMINAL_PATTERN = re.compile(
    r"\bOMX_TASK_CLOSED[^\n]*\b(?:done|blocked|fail|failed)\b|\bfinish:\s*(?:done|blocked|fail|failed)\b",
    re.I,
)
TMUX_CONTENT_STATE_PATTERNS = (
    (
        "failed",
        re.compile(r"\bOMX_TASK_CLOSED[^\n]*\b(?:fail|failed)\b|\bfinish:\s*(?:fail|failed)\b|\bGoal failed\b", re.I),
    ),
    (
        "blocked",
        re.compile(r"\bOMX_TASK_CLOSED[^\n]*\bblocked\b|\bfinish:\s*blocked\b", re.I),
    ),
    (
        "done",
        re.compile(r"\bOMX_TASK_CLOSED[^\n]*\bdone\b|\bfinish:\s*done\b|\bGoal achieved\b", re.I),
    ),
    (
        "running",
        TMUX_RUNNING_CONTENT_PATTERN,
    ),
    ("idle", re.compile(r"\bAsk anything\.\.\.|\bGoal paused\b", re.I)),
)


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def _title_count_key(state: str) -> str:
    return "done" if state == "done" else state


def _sanitize_tmux_title_text(value: str) -> str:
    chars: list[str] = []
    for char in str(value):
        code = ord(char)
        if char in {"\n", "\r", "\t"}:
            chars.append(" ")
        elif code < 32 or code == 127:
            continue
        else:
            chars.append(char)
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def format_tmux_session_title(
    payload: dict[str, Any],
    *,
    headline: str = "",
    max_chars: int = 80,
) -> str:
    aggregate = payload.get("tmux_session_aggregate") if isinstance(payload.get("tmux_session_aggregate"), dict) else {}
    state = str(aggregate.get("state") or "unknown").lower()
    counts = aggregate.get("counts") if isinstance(aggregate.get("counts"), dict) else {}
    worker_count = int(aggregate.get("worker_count", 0) or 0)
    if state == "unknown" and worker_count == 0:
        state = "idle"
    label = TMUX_SESSION_TITLE_LABELS.get(state, TMUX_SESSION_TITLE_LABELS["unknown"])
    state_count = worker_count if state == "idle" else int(counts.get(_title_count_key(state), 0) or 0)
    progress = f"{state_count}/{worker_count}" if worker_count else "0/0" if state == "idle" else "0"
    safe_headline = _sanitize_tmux_title_text(headline or str(payload.get("session_headline") or "tmux session"))
    title = f"{safe_headline} · {label} {progress}".strip()
    return title[: max(8, max_chars)]


def write_tmux_session_title_state(path: Path | str, title: str) -> Path:
    state_file = Path(path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(f"{state_file.name}.{os.getpid()}.tmp")
    tmp.write_text(f"{title}\n", encoding="utf-8")
    os.replace(tmp, state_file)
    return state_file


def read_tmux_session_title_state(path: Path | str) -> str:
    return _read_text_file(Path(path))


def cursor_title_state_path_for_tty(tty_path: str, *, cache_home: str | Path | None = None) -> Path:
    base = Path(cache_home or os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tty_path).replace("/", "_")) or "unknown"
    return base / "cursor-tab-title" / f"{key}.title"


def tmux_client_title_headline(session_name: str) -> str:
    value = _sanitize_tmux_title_text(session_name)
    prefix = "codex-visible-agent-orchestrator-"
    if value.startswith(prefix):
        return f"visible-orch {value.removeprefix(prefix)}"
    return value


def tmux_client_title_updates(
    clients: list[tuple[str, str, str]],
    payloads: dict[str, dict[str, Any]],
    *,
    last_titles: dict[str, str],
    observed_titles: dict[str, str],
    headline: str = "",
    max_chars: int = 80,
) -> list[tuple[str, str]]:
    updates: list[tuple[str, str]] = []
    empty_payload = {"tmux_session_aggregate": {"state": "unknown", "worker_count": 0, "counts": {}}}
    for client_tty, session_name, _active_pane in clients:
        title = format_tmux_session_title(
            payloads.get(session_name, empty_payload),
            headline=headline or tmux_client_title_headline(session_name),
            max_chars=max_chars,
        )
        if last_titles.get(client_tty) != title or observed_titles.get(client_tty) != title:
            updates.append((client_tty, title))
    return updates


def write_terminal_title_sequence(tty_path: str | Path, title: str) -> None:
    safe = _sanitize_tmux_title_text(title)
    sequence = f"\033]0;{safe}\007\033]1;{safe}\007\033]2;{safe}\007"
    with open(tty_path, "w", encoding="utf-8", errors="ignore") as handle:
        handle.write(sequence)


def summarize_tmux_agent_states(states: list[str]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    recognized_states = ("running", "done", "failed", "blocked", "idle", "unknown")
    for state in states:
        normalized = str(state or "").strip().lower()
        if normalized in recognized_states:
            counts[normalized] += 1
    worker_count = sum(int(counts[state]) for state in recognized_states)
    return {
        "state": tmux_session_state_from_counts(counts, worker_count),
        "worker_count": worker_count,
        "counts": {key: int(counts[key]) for key in recognized_states},
    }


def summarize_tmux_session_agent_panes(panes: list[dict[str, Any]]) -> dict[str, Any]:
    states: list[str] = []
    for pane in panes:
        state = tmux_agent_pane_state(
            str(pane.get("command") or ""),
            str(pane.get("title") or ""),
            str(pane.get("content") or ""),
        )
        if state:
            states.append(state)
    return summarize_tmux_agent_states(states)


def tmux_session_state_from_counts(counts: dict[str, int], worker_count: int) -> str:
    if worker_count <= 0:
        return "unknown"
    if counts.get("running", 0) > 0:
        return "running"
    if counts.get("idle", 0) > 0:
        return "idle"
    if counts.get("failed", 0) > 0:
        return "failed"
    if counts.get("blocked", 0) > 0:
        return "blocked"
    if counts.get("done", 0) == worker_count:
        return "done"
    return "unknown"


def tmux_agent_pane_state(command: str, title: str, content: str = "") -> str:
    cmd = Path(command).name.lower()
    clean_title = _sanitize_tmux_title_text(title)
    lower_title = clean_title.lower()
    if cmd in {"tmux-agent-sidebar"} or _is_subagent_sidebar_pane(cmd, content):
        return ""
    is_agent = (
        cmd in TMUX_AGENT_COMMANDS
        or clean_title.startswith(("🤖", "✅", "❌", "⚡", "🧭", "🧪", "🔎", "📝", "🔬"))
        or clean_title.startswith(TMUX_SPINNER_PREFIXES)
        or any(word in lower_title for word in TMUX_AGENT_TITLE_WORDS)
    )
    if not is_agent:
        return ""
    if clean_title.startswith("❌") or TMUX_FAILED_TITLE_MARKER.match(clean_title):
        return "failed"
    if TMUX_BLOCKED_TITLE_MARKER.match(clean_title):
        return "blocked"
    recent_content = _tmux_recent_content(content)
    content_state = tmux_agent_pane_content_state(recent_content)
    running_pos = max((match.start() for match in TMUX_RUNNING_CONTENT_PATTERN.finditer(recent_content)), default=-1)
    terminal_pos = max((match.start() for match in TMUX_PROTOCOL_TERMINAL_PATTERN.finditer(recent_content)), default=-1)
    if running_pos > terminal_pos:
        return "running"
    if clean_title.startswith("✅") or TMUX_DONE_TITLE_MARKER.match(clean_title):
        return "done"
    if content_state:
        return content_state
    if any(word in lower_title for word in TMUX_IDLE_TITLE_WORDS):
        return "idle"
    if clean_title.startswith("⚡") or clean_title.startswith(TMUX_SPINNER_PREFIXES):
        return "running"
    if any(word in lower_title for word in TMUX_RUNNING_TITLE_WORDS):
        return "running"
    return "idle"


def _is_subagent_sidebar_pane(cmd: str, content: str) -> bool:
    lower_content = str(content or "").lower()
    return cmd.startswith("python") and "subagents" in lower_content and "ledgers" in lower_content


def tmux_agent_pane_content_state(content: str) -> str:
    text = _tmux_recent_content(content)
    latest_state = ""
    latest_pos = -1
    for state, pattern in TMUX_CONTENT_STATE_PATTERNS:
        for match in pattern.finditer(text):
            if match.start() >= latest_pos:
                latest_state = state
                latest_pos = match.start()
    return latest_state


def _tmux_recent_content(content: str) -> str:
    lines = str(content or "").replace("\r", "\n").splitlines()
    return "\n".join(lines[-TMUX_CONTENT_STATE_TAIL_LINES:])


def tmux_session_agent_title_payload(
    session_name: str,
    *,
    tmux_command: str = "tmux",
) -> dict[str, Any]:
    return {
        "tmux_session_aggregate": summarize_tmux_session_agent_panes(
            list_tmux_session_agent_panes(session_name, tmux_command=tmux_command),
        )
    }


def list_tmux_session_agent_panes(session_name: str, *, tmux_command: str = "tmux") -> list[dict[str, str]]:
    panes = _list_tmux_session_agent_panes_all_windows(session_name, tmux_command=tmux_command)
    if panes:
        return panes
    proc = subprocess.run(
        [tmux_command, "list-panes", "-t", session_name, "-F", "#{pane_id}\t#{pane_current_command}\t#{pane_title}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    panes: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        pane_id, sep, rest = line.partition("\t")
        command, sep2, title = rest.partition("\t")
        if sep and sep2:
            panes.append(
                {
                    "pane_id": pane_id,
                    "command": command,
                    "title": title,
                    "content": _capture_tmux_pane_tail(pane_id, tmux_command=tmux_command),
                }
            )
    return panes


def list_tmux_pane_metadata(*, tmux_command: str = "tmux") -> list[dict[str, str]]:
    try:
        proc = subprocess.run(
            [tmux_command, "list-panes", "-a", "-F", TMUX_PANE_METADATA_FORMAT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"tmux list-panes failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"tmux list-panes failed: {detail}")
    panes: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 6:
            raise RuntimeError(f"malformed tmux pane metadata: {line!r}")
        session_name, pane_id, history_bytes, command, title, dead = parts
        panes.append(
            {
                "session_name": session_name,
                "pane_id": pane_id,
                "history_bytes": history_bytes,
                "command": command,
                "title": title,
                "dead": dead,
            }
        )
    return panes


def tmux_pane_fingerprint(pane: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(pane.get("session_name") or ""),
        str(pane.get("history_bytes") or ""),
        str(pane.get("command") or ""),
        str(pane.get("title") or ""),
        str(pane.get("dead") or ""),
    )


def _list_tmux_session_agent_panes_all_windows(session_name: str, *, tmux_command: str) -> list[dict[str, str]]:
    proc = subprocess.run(
        [
            tmux_command,
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{pane_id}\t#{pane_current_command}\t#{pane_title}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    panes: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        pane_session, pane_id, command, title = parts
        if pane_session != session_name:
            continue
        panes.append(
            {
                "pane_id": pane_id,
                "command": command,
                "title": title,
                "content": _capture_tmux_pane_tail(pane_id, tmux_command=tmux_command),
            }
        )
    return panes


def capture_tmux_pane_tail_checked(pane_id: str, *, tmux_command: str = "tmux") -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [tmux_command, "capture-pane", "-p", "-t", pane_id, "-S", f"-{TMUX_CAPTURE_TAIL_LINES}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return (False, "")
    if proc.returncode != 0:
        return (False, "")
    return (True, proc.stdout)


@dataclass
class TmuxLatestStateWatcher:
    tmux_command: str = "tmux"
    pane_fingerprints: dict[str, tuple[str, str, str, str, str]] = field(default_factory=dict)
    pane_states: dict[str, str] = field(default_factory=dict)
    pane_sessions: dict[str, str] = field(default_factory=dict)
    idle_pending: set[str] = field(default_factory=set)
    capture_retry: set[str] = field(default_factory=set)
    metadata_failures: int = 0

    def poll(self) -> dict[str, dict[str, Any]]:
        try:
            panes = list_tmux_pane_metadata(tmux_command=self.tmux_command)
        except RuntimeError:
            self.metadata_failures += 1
            return self._payloads(force_unknown=self.metadata_failures >= 3)
        self.metadata_failures = 0
        current_panes = {str(pane.get("pane_id") or ""): pane for pane in panes if str(pane.get("pane_id") or "")}
        current_ids = set(current_panes)
        stale_ids = (
            set(self.pane_fingerprints)
            | set(self.pane_states)
            | set(self.pane_sessions)
            | set(self.idle_pending)
            | set(self.capture_retry)
        )
        for pane_id in stale_ids - current_ids:
            self.pane_fingerprints.pop(pane_id, None)
            self.pane_states.pop(pane_id, None)
            self.pane_sessions.pop(pane_id, None)
            self.idle_pending.discard(pane_id)
            self.capture_retry.discard(pane_id)

        for pane_id, pane in current_panes.items():
            fingerprint = tmux_pane_fingerprint(pane)
            if (
                self.pane_fingerprints.get(pane_id) == fingerprint
                and pane_id not in self.pane_states
                and pane_id not in self.idle_pending
                and pane_id not in self.capture_retry
            ):
                continue
            ok, content = capture_tmux_pane_tail_checked(pane_id, tmux_command=self.tmux_command)
            if not ok:
                self.capture_retry.add(pane_id)
                if pane_id not in self.pane_sessions:
                    self.pane_sessions[pane_id] = str(pane.get("session_name") or "")
                continue
            self.capture_retry.discard(pane_id)
            state = tmux_agent_pane_state(
                str(pane.get("command") or ""),
                str(pane.get("title") or ""),
                content,
            )
            previous_state = self.pane_states.get(pane_id)
            self.pane_fingerprints[pane_id] = fingerprint
            self.pane_sessions[pane_id] = str(pane.get("session_name") or "")
            if state:
                if state == "idle" and previous_state == "running" and pane_id not in self.idle_pending:
                    self.idle_pending.add(pane_id)
                    self.pane_states[pane_id] = "running"
                else:
                    self.idle_pending.discard(pane_id)
                    self.pane_states[pane_id] = state
            else:
                self.idle_pending.discard(pane_id)
                self.pane_states.pop(pane_id, None)
        return self._payloads()

    def _payloads(self, *, force_unknown: bool = False) -> dict[str, dict[str, Any]]:
        session_states: dict[str, list[str]] = {}
        for session_name in self.pane_sessions.values():
            if session_name:
                session_states.setdefault(session_name, [])
        for pane_id, state in self.pane_states.items():
            session_name = self.pane_sessions.get(pane_id, "")
            if not session_name:
                continue
            session_states.setdefault(session_name, []).append(state)
        payloads: dict[str, dict[str, Any]] = {}
        for session_name, states in session_states.items():
            aggregate = summarize_tmux_agent_states(states)
            if force_unknown:
                worker_count = int(aggregate.get("worker_count") or 0)
                aggregate = {
                    "state": "unknown",
                    "worker_count": worker_count,
                    "counts": {
                        "running": 0,
                        "done": 0,
                        "failed": 0,
                        "blocked": 0,
                        "idle": 0,
                        "unknown": worker_count,
                    },
                }
            payloads[session_name] = {"tmux_session_aggregate": aggregate}
        return payloads


def _capture_tmux_pane_tail(pane_id: str, *, tmux_command: str) -> str:
    ok, content = capture_tmux_pane_tail_checked(pane_id, tmux_command=tmux_command)
    if not ok:
        return ""
    return content
