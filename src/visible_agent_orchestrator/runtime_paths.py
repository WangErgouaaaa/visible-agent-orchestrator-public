"""Portable runtime and workspace path selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


RUNTIME_ROOT_ENV = "VAO_ROOT"
WORKSPACE_ROOT_ENV = "VAO_WORKSPACE_ROOT"


def runtime_root(
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an explicit root, then ``VAO_ROOT``, then ``~/vao-runtime``."""
    values = os.environ if env is None else env
    selected = value if value is not None and str(value).strip() else values.get(RUNTIME_ROOT_ENV, "")
    if not str(selected).strip():
        selected = Path.home() / "vao-runtime"
    return Path(selected).expanduser().resolve()


def workspace_root(
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an explicit workspace, then its independent portable default."""
    values = os.environ if env is None else env
    selected = value if value is not None and str(value).strip() else values.get(WORKSPACE_ROOT_ENV, "")
    if not str(selected).strip():
        selected = Path.home() / "vao-workspace"
    return Path(selected).expanduser().resolve()


def framework_root(
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return runtime_root(value, env=env) / ".omx" / "framework" / "team"


def state_team_root(
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return runtime_root(value, env=env) / ".omx" / "state" / "team"


def runtime_bin(
    name: str,
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return runtime_root(value, env=env) / "bin" / name
