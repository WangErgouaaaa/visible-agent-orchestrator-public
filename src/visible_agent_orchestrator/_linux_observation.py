from collections import deque
from pathlib import Path
import re


def process_record(pid: int) -> dict:
    path = Path("/proc") / str(int(pid))
    try:
        before = (path / "stat").read_text()
        cmdline = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        after = (path / "stat").read_text()
        left = before[before.rfind(")") + 1:].split()
        right = after[after.rfind(")") + 1:].split()
        # stat's comm may contain spaces/parentheses; field 22 identifies PID reuse.
        if (before[:before.rfind(")")], left[1:3], left[19]) != (after[:after.rfind(")")], right[1:3], right[19]):
            raise ValueError("process identity changed")
        if right[0] in {"Z", "X", "x"} or not cmdline:
            raise ValueError("process is not live")
        parent, group, start = int(right[1]), int(right[2]), int(right[19])
        comm = after[after.find("(") + 1:after.rfind(")")]
        return {"pid": int(pid), "ppid": parent, "start_time_ticks": start,
                "returncode": 0, "stderr": "", "cmdline": cmdline,
                "ps": f"{pid} {parent} {group} {right[0]} {comm} {cmdline}"}
    except (OSError, ValueError, IndexError):
        return {"pid": int(pid), "returncode": 1, "stderr": "process unavailable or changed", "cmdline": "", "ps": ""}


def process_instance_state(pid: int, expected_start_time_ticks: int) -> str:
    """'alive' when /proc/<pid> carries exactly the expected start-time
    ticks; 'dead' when the pid is positively absent or carries different
    ticks (a recycled pid number); 'unknown' when the observation cannot be
    made (unreadable or malformed /proc entry). A recycled pid never fakes
    liveness, and an unreadable observation is never proof of death (#934)."""
    if int(pid or 0) <= 0 or int(expected_start_time_ticks or 0) <= 0:
        return "unknown"
    try:
        text = (Path("/proc") / str(int(pid)) / "stat").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return "dead"
    except (OSError, TypeError, ValueError):
        return "unknown"
    close = text.rfind(")")
    if close < 0:
        return "unknown"
    fields = text[close + 1 :].strip().split()
    if len(fields) < 20:
        return "unknown"
    try:
        observed = int(fields[19])
    except (TypeError, ValueError):
        return "unknown"
    return "alive" if observed == int(expected_start_time_ticks) else "dead"


def process_tree(root_pid: int, max_depth: int = 5) -> dict:
    nodes = {}
    pending = deque([(int(root_pid), 0, None)])
    seen = set()
    while pending:
        pid, depth, parent = pending.popleft()
        if pid in seen or depth > max_depth:
            continue
        seen.add(pid)
        record = process_record(pid)
        if record["returncode"] or parent is not None and record["ppid"] != parent:
            continue
        record.update(depth=depth, children=[])
        nodes[str(pid)] = record
        if depth >= max_depth:
            continue
        try:
            children = set()
            paths = list((Path("/proc") / str(pid) / "task").glob("*/children"))
            if not paths:
                raise ValueError("process children unavailable")
            for path in paths:
                children.update(int(value) for value in path.read_text().split())
            pending.extend((child, depth + 1, pid) for child in sorted(children))
        except (OSError, ValueError):
            del nodes[str(pid)]
    # Recheck selected nodes, not other panes, before accepting their ancestry.
    for key, record in list(nodes.items()):
        fresh = process_record(record["pid"])
        parent = str(record["ppid"])
        if (fresh.get("start_time_ticks"), fresh.get("ppid"), fresh.get("cmdline")) != (record["start_time_ticks"], record["ppid"], record["cmdline"]) or record["depth"] and parent not in nodes:
            del nodes[key]
    edges = []
    for record in nodes.values():
        if record["depth"]:
            nodes[str(record["ppid"])]["children"].append(record["pid"])
            edges.append({"parent": record["ppid"], "child": record["pid"]})
    return {"root_pid": int(root_pid), "max_depth": max_depth, "nodes": nodes, "edges": edges}


def target_listeners(tool: str, output: str, port: int) -> tuple[list[dict], bool]:
    matches = []
    complete = True
    for line in output.splitlines():
        if not re.search(rf":{int(port)}(?=\s|$)", line):
            if tool == "ss" and line.strip():
                complete = False
            continue
        parts = line.split()
        pids = []
        valid = False
        if tool == "ss":
            pids = sorted({int(pid) for pid in re.findall(r"pid=(\d+)", line)})
            owners = re.search(r"users:\((.*)\)$", line)
            owner_pattern = r'\("(?:[^"\\]|\\.)*",pid=[1-9]\d*,fd=\d+\)'
            valid = bool(len(parts) >= 6 and parts[0] == "LISTEN" and parts[1].isdigit() and parts[2].isdigit() and parts[3].endswith(f":{int(port)}") and owners and re.fullmatch(rf"{owner_pattern}(?:,{owner_pattern})*", owners[1]))
        elif tool == "lsof":
            valid = bool(len(parts) >= 10 and parts[1].isdigit() and parts[-3] == "TCP" and parts[-2].endswith(f":{int(port)}") and parts[-1] == "(LISTEN)")
            if valid:
                pids = [int(parts[1])]
        elif tool == "netstat":
            valid = bool(len(parts) >= 7 and parts[0] in {"tcp", "tcp6"} and parts[3].endswith(f":{int(port)}") and parts[5] == "LISTEN")
            owner = re.match(r"([1-9]\d*)/\S+", parts[6]) if valid else None
            valid = bool(valid and owner)
            if owner:
                pids = [int(owner[1])]
        complete = complete and valid and bool(pids)
        matches.append({"command": tool, "line": line, "pids": pids, "ports": [int(port)]})
    owners = {pid for match in matches for pid in match["pids"]}
    return matches, bool(matches and complete and len(owners) == 1)
