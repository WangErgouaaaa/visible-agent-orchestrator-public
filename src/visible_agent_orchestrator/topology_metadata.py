"""Formal chapter-level topology metadata contract for orchestration hints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TOPOLOGY_TO_INNER_MODE = {
    "1x3": "4x3",
    "1x5": "4x7",
    "1x7": "4x7",
    "4x3": "4x3",
    "4x7": "4x7",
}
TOPOLOGY_FIELD_NAMES = (
    "recommended_inner_topology",
    "recommended_topology",
    "inner_topology",
    "inner_mode",
)


class TopologyMetadataError(Exception):
    """Operator-facing topology metadata validation error."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TopologyMetadataError(f"topology metadata must be valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise TopologyMetadataError(f"topology metadata must be a JSON object: {path}")
    return data


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _requested_topology(payload: dict[str, Any]) -> str:
    for key in TOPOLOGY_FIELD_NAMES:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def resolve_topology_metadata(path_value: str | Path | None) -> dict[str, Any]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return {}
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise TopologyMetadataError(f"topology metadata file not found: {path}")

    payload = _read_json(path)
    schema_version = int(payload.get("schema_version", SCHEMA_VERSION) or 0)
    if schema_version != SCHEMA_VERSION:
        raise TopologyMetadataError(
            f"topology metadata schema_version must be {SCHEMA_VERSION}: {path}"
        )

    chapter = str(payload.get("chapter") or payload.get("chapter_id") or payload.get("section") or "").strip()
    if not chapter:
        raise TopologyMetadataError(f"topology metadata missing chapter: {path}")

    requested = _requested_topology(payload)
    if not requested:
        raise TopologyMetadataError(
            f"topology metadata missing recommended_inner_topology: {path}"
        )

    resolved_inner_mode = TOPOLOGY_TO_INNER_MODE.get(requested)
    if not resolved_inner_mode:
        raise TopologyMetadataError(f"unsupported topology metadata value '{requested}' in {path}")

    return {
        "schema_version": schema_version,
        "source_path": str(path),
        "chapter": chapter,
        "recommended_roles": _normalize_string_list(payload.get("recommended_roles")),
        "requested_topology": requested,
        "resolved_inner_mode": resolved_inner_mode,
        "summary": " ".join(str(payload.get("summary") or "").split()),
        "resolution_note": (
            "direct"
            if requested in {"1x3", "4x3", "4x7"}
            else "widened_to_supported_mode"
        ),
    }


def render_topology_metadata_context(metadata: dict[str, Any]) -> str:
    if not metadata:
        return ""
    lines = [
        "",
        "## Chapter Topology Metadata",
        "",
        f"- source_path: {metadata.get('source_path') or '-'}",
        f"- chapter: {metadata.get('chapter') or '-'}",
        f"- recommended_inner_topology: {metadata.get('requested_topology') or '-'}",
        f"- resolved_inner_mode: {metadata.get('resolved_inner_mode') or '-'}",
        f"- recommended_roles: {', '.join(metadata.get('recommended_roles', [])) or '-'}",
    ]
    if metadata.get("summary"):
        lines.append(f"- summary: {metadata.get('summary')}")
    if metadata.get("resolution_note") == "widened_to_supported_mode":
        lines.append("- note: current runtime supports 4x3 and 4x7 only, so this hint was widened conservatively.")
    return "\n".join(lines)
