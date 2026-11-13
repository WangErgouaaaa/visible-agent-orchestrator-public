"""Import-path bootstrap helpers for source and installed script layouts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ENV_LIB = "OMX_VISIBLE_AGENT_ORCHESTRATOR_LIB"


def _append_unique(paths: list[Path], path: Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved not in paths:
        paths.append(resolved)


def candidate_paths(anchor_file: str | Path) -> list[Path]:
    """Return import roots for env override, source checkout, and live install."""

    anchor = Path(anchor_file).expanduser().resolve()
    roots: list[Path] = []

    env_lib = os.environ.get(ENV_LIB)
    if env_lib:
        _append_unique(roots, Path(env_lib))

    base = anchor.parent.parent
    _append_unique(roots, base / "src")
    _append_unique(roots, base / "lib")
    return roots


def bootstrap_repo_or_live_path(anchor_file: str | Path) -> list[str]:
    """Insert existing candidate import roots once and return inserted paths."""

    added: list[str] = []
    for path in candidate_paths(anchor_file):
        path_str = str(path)
        if path.exists() and path_str not in sys.path:
            sys.path.insert(0, path_str)
            added.append(path_str)
    return added


def ensure_import_path(anchor_file: str | Path) -> None:
    """Ensure source-repo or live-runtime package imports are available."""

    bootstrap_repo_or_live_path(anchor_file)
