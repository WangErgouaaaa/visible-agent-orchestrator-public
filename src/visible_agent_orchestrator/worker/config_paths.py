"""Live-enable configuration path discovery for worker backend tooling."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

from visible_agent_orchestrator.runtime.errors import RuntimeAdapterError

from .live_enable import LiveEnableConfig


ENV_LIVE_ENABLE_CONFIG = "OMX_WORKER_BACKEND_LIVE_ENABLE_CONFIG"
DEFAULT_CONFIG_REL = Path("worker-backends/live-enable.default.disabled.json")


@dataclass(frozen=True)
class ConfigPathCandidate:
    source: str
    path: str
    found: bool
    explicit: bool = False
    env_var: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ConfigPathEvidence:
    config_path: str
    config_source: str
    config_found: bool
    config_missing: bool
    fallback_used: bool
    fail_closed_due_to_missing_config: bool
    candidates: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def discover_config_paths(
    *,
    explicit_config: Path | str | None = None,
    root: Path | str | None = None,
    source_root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ConfigPathCandidate]:
    values = os.environ if env is None else env
    candidates: list[ConfigPathCandidate] = []
    if explicit_config:
        path = Path(explicit_config).expanduser().resolve()
        candidates.append(ConfigPathCandidate("explicit", str(path), path.is_file(), explicit=True))
    env_value = values.get(ENV_LIVE_ENABLE_CONFIG, "")
    if env_value:
        path = Path(env_value).expanduser().resolve()
        candidates.append(ConfigPathCandidate("env", str(path), path.is_file(), env_var=ENV_LIVE_ENABLE_CONFIG))
    if root is not None:
        path = Path(root).expanduser().resolve() / DEFAULT_CONFIG_REL
        candidates.append(ConfigPathCandidate("root-default", str(path), path.is_file()))
    if source_root is not None:
        path = Path(source_root).expanduser().resolve() / DEFAULT_CONFIG_REL
        if str(path) not in {item.path for item in candidates}:
            candidates.append(ConfigPathCandidate("source-default", str(path), path.is_file()))
    return candidates


def resolve_live_enable_config(
    backend_kind: str,
    *,
    explicit_config: Path | str | None = None,
    root: Path | str | None = None,
    source_root: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[LiveEnableConfig, ConfigPathEvidence]:
    candidates = discover_config_paths(
        explicit_config=explicit_config,
        root=root,
        source_root=source_root,
        env=env,
    )
    if candidates and candidates[0].source in {"explicit", "env"} and not candidates[0].found:
        return _missing_config(backend_kind, candidates[0], candidates)
    for candidate in candidates:
        if candidate.found:
            try:
                config = _load_file_config(Path(candidate.path), backend_kind)
            except RuntimeAdapterError:
                return _missing_config(backend_kind, candidate, candidates)
            return config, ConfigPathEvidence(
                config_path=candidate.path,
                config_source=candidate.source,
                config_found=True,
                config_missing=False,
                fallback_used=False,
                fail_closed_due_to_missing_config=False,
                candidates=[item.to_dict() for item in candidates],
                warnings=[],
            )
    fallback = ConfigPathCandidate("package-default", "package-disabled-default", False)
    return _missing_config(backend_kind, fallback, candidates)


def _load_file_config(path: Path, backend_kind: str) -> LiveEnableConfig:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeAdapterError(f"live enable config missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeAdapterError(f"invalid live enable config: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeAdapterError(f"live enable config must contain an object: {path}")
    return LiveEnableConfig.from_dict(payload, backend_kind)


def _missing_config(
    backend_kind: str,
    selected: ConfigPathCandidate,
    candidates: list[ConfigPathCandidate],
) -> tuple[LiveEnableConfig, ConfigPathEvidence]:
    warnings = [
        "live enable config missing; using disabled package fallback",
        "missing live enable config fails closed",
    ]
    all_candidates = [item.to_dict() for item in candidates]
    if selected.source == "package-default":
        all_candidates.append(selected.to_dict())
    return LiveEnableConfig.disabled_default(backend_kind), ConfigPathEvidence(
        config_path=selected.path,
        config_source=selected.source,
        config_found=False,
        config_missing=True,
        fallback_used=True,
        fail_closed_due_to_missing_config=True,
        candidates=all_candidates,
        warnings=warnings,
    )


__all__ = [
    "ConfigPathCandidate",
    "ConfigPathEvidence",
    "DEFAULT_CONFIG_REL",
    "ENV_LIVE_ENABLE_CONFIG",
    "discover_config_paths",
    "resolve_live_enable_config",
]
