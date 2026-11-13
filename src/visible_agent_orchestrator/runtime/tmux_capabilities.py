"""tmux baseline and command-shape guardrails for the active live mainline."""

from __future__ import annotations

from typing import Final


TMUX_LIVE_BASELINE: Final[str] = "3.4"
ACTIVE_RUNTIME_SUBSTRATE: Final[str] = "tmux"
TMUX_REFERENCE_BACKEND: Final[str] = "tmux-reference"

BLOCKED_POST_BASELINE_FEATURES: Final[tuple[str, ...]] = (
    "display-popup -k",
    "selection-mode",
    "tiled-layout-max-columns",
    "extended-keys-format",
    "allow-set-title",
    "refresh-client -r",
)

_OPTION_SPEC: Final[dict[str, dict[str, bool]]] = {
    "display-message": {
        "-a": False,
        "-I": False,
        "-l": False,
        "-N": False,
        "-p": False,
        "-v": False,
        "-c": True,
        "-d": True,
        "-F": True,
        "-t": True,
    },
    "display-popup": {
        "-B": False,
        "-C": False,
        "-E": False,
        "-b": True,
        "-c": True,
        "-d": True,
        "-e": True,
        "-h": True,
        "-s": True,
        "-S": True,
        "-t": True,
        "-T": True,
        "-w": True,
        "-x": True,
        "-y": True,
    },
    "list-panes": {
        "-a": False,
        "-s": False,
        "-F": True,
        "-f": True,
        "-t": True,
    },
    "new-window": {
        "-a": False,
        "-b": False,
        "-d": False,
        "-k": False,
        "-P": False,
        "-S": False,
        "-c": True,
        "-e": True,
        "-F": True,
        "-n": True,
        "-t": True,
    },
    "respawn-pane": {
        "-k": False,
        "-t": True,
        "-c": True,
    },
    "select-layout": {
        "-E": False,
        "-n": False,
        "-o": False,
        "-p": False,
        "-t": True,
    },
    "select-pane": {
        "-D": False,
        "-d": False,
        "-e": False,
        "-L": False,
        "-l": False,
        "-m": False,
        "-M": False,
        "-R": False,
        "-T": True,
        "-t": True,
        "-U": False,
        "-Z": False,
    },
    "split-window": {
        "-b": False,
        "-d": False,
        "-e": True,
        "-f": False,
        "-F": True,
        "-h": False,
        "-I": False,
        "-P": False,
        "-p": True,
        "-t": True,
        "-v": False,
        "-Z": False,
        "-c": True,
        "-l": True,
    },
    "wait-for": {
        "-L": False,
        "-S": False,
        "-U": False,
    },
}

_BLOCKED_COMMANDS: Final[dict[str, str]] = {
    "selection-mode": "selection-mode requires tmux 3.6; the active live mainline is pinned to the tmux 3.4 baseline",
}

_BLOCKED_FLAGS: Final[dict[tuple[str, str], str]] = {
    ("display-popup", "-k"): "display-popup -k requires tmux 3.6; the active live mainline is pinned to the tmux 3.4 baseline",
}


def _command_reason(command: str, flag: str) -> str:
    return _BLOCKED_FLAGS[(command, flag)]


def command_shape_supported(command: str, argv_tail: list[str]) -> tuple[bool, str]:
    command = str(command or "").strip()
    if command in _BLOCKED_COMMANDS:
        return False, _BLOCKED_COMMANDS[command]
    spec = _OPTION_SPEC.get(command)
    if spec is None:
        return True, ""
    index = 0
    while index < len(argv_tail):
        token = str(argv_tail[index])
        if not token.startswith("-") or token == "-":
            break
        if (command, token) in _BLOCKED_FLAGS:
            return False, _command_reason(command, token)
        if token not in spec:
            return False, (
                f"{command} {token} is outside the approved tmux {TMUX_LIVE_BASELINE} mainline command shape"
            )
        if spec[token]:
            index += 1
            if index >= len(argv_tail):
                return False, f"{command} {token} requires an argument"
        index += 1
    return True, ""


def command_support_report() -> dict[str, object]:
    return {
        "tmux_live_baseline": TMUX_LIVE_BASELINE,
        "active_runtime_substrate": ACTIVE_RUNTIME_SUBSTRATE,
        "tmux_reference_backend": TMUX_REFERENCE_BACKEND,
        "blocked_post_baseline_features": list(BLOCKED_POST_BASELINE_FEATURES),
    }
