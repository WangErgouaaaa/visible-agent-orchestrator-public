import json
import re
import shlex
import sys
from pathlib import Path

if __package__:
    from .runtime_paths import runtime_bin
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from visible_agent_orchestrator.runtime_paths import runtime_bin


MAX_INPUT_BYTES = 65536
# Literal words only. Reject shell expansion/operators even inside quotes rather
# than trying to interpret a general shell program. Quotes may enclose a root
# containing spaces; backslashes, newlines, globs and substitutions are excluded.
WORD = r'''(?:[A-Za-z0-9_./:=+\-]+|'[A-Za-z0-9_./:=+ \-]+'|"[A-Za-z0-9_./:=+ \-]+")'''
SIMPLE_COMMAND = re.compile(rf"[ \t]*{WORD}(?:[ \t]+{WORD})*[ \t]*")


def compact_command(command, *, env=None):
    if not isinstance(command, str) or not SIMPLE_COMMAND.fullmatch(command):
        return None
    words = shlex.split(command)
    executable = str(runtime_bin("omx_visible_dispatch", env=env))
    if words[:2] != [executable, "status"]:
        return None
    seen = set()
    index = 2
    while index < len(words):
        option = words[index]
        if option in seen:
            return None
        seen.add(option)
        if option in ("--json", "--include-pool-readiness"):
            index += 1
        elif option in ("--root", "--backend"):
            if index + 1 == len(words):
                return None
            value = words[index + 1]
            if option == "--root" and (not value.strip() or value.startswith("-")):
                return None
            if option == "--backend" and value != "tmux-reference":
                return None
            index += 2
        else:
            return None
    if not {"--root", "--json"} <= seen:
        return None
    return command + " --compact"


def rewrite(payload):
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return {}
    key = {
        "exec_command": "cmd",
        "functions.exec_command": "cmd",
        "Bash": "command",
    }.get(payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None)
    tool_input = payload.get("tool_input")
    if key is None or not isinstance(tool_input, dict):
        return {}
    replacement = compact_command(tool_input.get(key))
    if replacement is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, key: replacement},
        }
    }


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    output = "{}"
    if len(raw) <= MAX_INPUT_BYTES:
        try:
            response = rewrite(json.loads(raw, parse_constant=reject_constant))
            output = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
        except (ValueError, UnicodeError, RecursionError):
            pass
    print(output)


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


if __name__ == "__main__":
    main()
