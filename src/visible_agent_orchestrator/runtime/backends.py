"""Runtime backend registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapter import RuntimeAdapter
from .errors import RuntimeAdapterError
from .rmux_reference import RmuxReferenceAdapter
from .rmux_spike import RmuxSpikeAdapter
from .tmux_reference import TmuxReferenceAdapter


def available_backends() -> list[dict[str, Any]]:
    return [
        {
            "name": "tmux-reference",
            "backend_type": "tmux",
            "status": "current/default",
            "default": True,
            "live_enabled": True,
            "fixture_only": False,
        },
        {
            "name": "rmux-reference",
            "backend_type": "rmux",
            "status": "candidate/read-only",
            "default": False,
            "live_enabled": False,
            "fixture_only": False,
        },
        {
            "name": "rmux-spike",
            "backend_type": "rmux",
            "status": "candidate/fixture-only",
            "default": False,
            "live_enabled": False,
            "fixture_only": True,
        },
    ]


def default_backend_name() -> str:
    return "tmux-reference"


def get_backend(name: str, fixture_dir: Path | None = None) -> RuntimeAdapter:
    if name == "tmux-reference":
        return TmuxReferenceAdapter(fixture_dir=fixture_dir)
    if name == "rmux-reference":
        return RmuxReferenceAdapter(fixture_dir=fixture_dir)
    if name == "rmux-spike":
        return RmuxSpikeAdapter(fixture_dir=fixture_dir)
    raise RuntimeAdapterError(f"backend is not available: {name}")


def is_backend_live_enabled(name: str) -> bool:
    for backend in available_backends():
        if backend["name"] == name:
            return bool(backend["live_enabled"])
    raise RuntimeAdapterError(f"backend is not available: {name}")
