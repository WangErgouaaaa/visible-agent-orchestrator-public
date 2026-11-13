"""Strict semantic-only collaboration profiles for supervisor runs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PROFILE_PRIMITIVES = {"pipeline", "fanout", "fork_join", "advisory_feedback"}
NODE_KINDS = {
    "execution_lane",
    "advisory_lane",
    "join_barrier",
    "advisory_bundle",
    "adoption_decision",
    "verification_gate",
    "integration_candidate",
    "human_decision",
}
OUTPUT_SEMANTICS = {
    "protocol_terminal",
    "advisory",
    "adoption_decision",
    "verification_result",
    "implementation",
}
EFFECT_CLASSES = {
    "observe",
    "artifact_only",
    "local_no_live",
    "read_only_worker",
    "write_isolated",
    "shared_write",
}
DECISION_OWNERS = {"main", "human", "deterministic_gate"}
JOIN_KINDS = {"all_of", "quorum"}
PROOF_KINDS = {
    "terminal_success",
    "artifact_present",
    "advisory_ready",
    "main_adoption_present",
    "gate_passed",
}
FORBIDDEN_FIELDS = {
    "worker_id",
    "pane_id",
    "session_id",
    "authorization",
    "authorization_id",
    "authorization_grant",
    "allow_live",
}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CollaborationProfileError(ValueError):
    """Raised when a collaboration profile is malformed or over-authoritative."""


def load_collaboration_profile(path_value: Path | str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollaborationProfileError(
            f"collaboration profile must be valid JSON: {path}"
        ) from exc
    profile = validate_collaboration_profile(payload)
    profile["source_path"] = str(path)
    return profile


def validate_collaboration_profile(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CollaborationProfileError("collaboration profile must be a JSON object")
    _reject_forbidden_fields(payload)

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CollaborationProfileError(
            f"collaboration profile schema_version must be {SCHEMA_VERSION}"
        )
    profile_id = _safe_id(payload.get("profile_id"), "profile_id")
    primitives = _string_list(payload.get("primitives"), "primitives")
    if not primitives or any(item not in PROFILE_PRIMITIVES for item in primitives):
        raise CollaborationProfileError("collaboration profile primitives are invalid")
    if len(set(primitives)) != len(primitives):
        raise CollaborationProfileError("collaboration profile primitives must be unique")

    nodes = _normalize_nodes(payload.get("nodes"))
    node_ids = {node["node_id"] for node in nodes}
    edges = _normalize_edges(payload.get("edges"), node_ids)
    _validate_cycles(nodes, edges, primitives, payload.get("bounded_feedback"))

    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "primitives": primitives,
        "nodes": nodes,
        "edges": edges,
        "bounded_feedback": _normalize_feedback(payload.get("bounded_feedback"), nodes),
        "live_authority": False,
    }


def collaboration_profile_digest(profile: dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in profile.items()
        if key != "source_path"
    }
    canonical = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def profile_edge(profile: dict[str, Any], edge_id: str) -> dict[str, Any]:
    for edge in profile.get("edges", []):
        if isinstance(edge, dict) and edge.get("edge_id") == edge_id:
            return dict(edge)
    raise CollaborationProfileError(f"collaboration profile edge not found: {edge_id}")


def _reject_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "live_authority":
                if nested is not False:
                    raise CollaborationProfileError(
                        "collaboration profile live_authority must be false"
                    )
                continue
            if key in FORBIDDEN_FIELDS:
                raise CollaborationProfileError(
                    f"collaboration profile must not contain {key}"
                )
            _reject_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_fields(nested)


def _normalize_nodes(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise CollaborationProfileError("collaboration profile nodes must be non-empty")
    nodes: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise CollaborationProfileError("collaboration profile node must be an object")
        node_id = _safe_id(raw.get("node_id"), "node_id")
        if node_id in seen:
            raise CollaborationProfileError(f"duplicate collaboration profile node: {node_id}")
        kind = _enum(raw.get("kind"), NODE_KINDS, "node kind")
        output_semantics = _enum(
            raw.get("output_semantics"),
            OUTPUT_SEMANTICS,
            "node output_semantics",
        )
        nodes.append(
            {
                "node_id": node_id,
                "kind": kind,
                "output_semantics": output_semantics,
            }
        )
        seen.add(node_id)
    return nodes


def _normalize_edges(value: object, node_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise CollaborationProfileError("collaboration profile edges must be non-empty")
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise CollaborationProfileError("collaboration profile edge must be an object")
        edge_id = _safe_id(raw.get("edge_id"), "edge_id")
        if edge_id in seen:
            raise CollaborationProfileError(f"duplicate collaboration profile edge: {edge_id}")
        source_nodes = _string_list(raw.get("source_nodes"), "source_nodes")
        if not source_nodes or any(node_id not in node_ids for node_id in source_nodes):
            raise CollaborationProfileError(f"edge {edge_id} has unknown source node")
        if len(set(source_nodes)) != len(source_nodes):
            raise CollaborationProfileError(f"edge {edge_id} source_nodes must be unique")
        target_node = _safe_id(raw.get("target_node"), "target_node")
        if target_node not in node_ids:
            raise CollaborationProfileError(f"edge {edge_id} has unknown target node")
        join = _normalize_join(raw.get("join"), source_nodes, edge_id)
        proof = _normalize_proof(raw.get("proof_requirements"), node_ids, edge_id)
        effect_class = _enum(raw.get("effect_class"), EFFECT_CLASSES, "effect_class")
        if (
            effect_class in {"read_only_worker", "write_isolated", "shared_write"}
            and any(item.get("kind") == "advisory_ready" for item in proof)
        ):
            raise CollaborationProfileError(
                f"edge {edge_id} advisory evidence cannot authorize a worker effect"
            )
        decision_owner = _enum(
            raw.get("decision_owner"),
            DECISION_OWNERS,
            "decision_owner",
        )
        on_failure = _enum(raw.get("on_failure"), {"escalate", "stop"}, "on_failure")
        supersession_scope = _enum(
            raw.get("supersession_scope"),
            {"workflow_revision"},
            "supersession_scope",
        )
        edges.append(
            {
                "edge_id": edge_id,
                "source_nodes": source_nodes,
                "target_node": target_node,
                **join,
                "proof_requirements": proof,
                "effect_class": effect_class,
                "decision_owner": decision_owner,
                "on_failure": on_failure,
                "supersession_scope": supersession_scope,
            }
        )
        seen.add(edge_id)
    return edges


def _normalize_join(
    value: object,
    source_nodes: list[str],
    edge_id: str,
) -> dict[str, Any]:
    if value == "all_of":
        return {
            "join": "all_of",
            "required_successes": len(source_nodes),
            "optional_source_nodes": [],
        }
    if not isinstance(value, dict):
        raise CollaborationProfileError(
            f"edge {edge_id} join must be all_of or a fixed join object"
        )
    kind = _enum(value.get("type"), JOIN_KINDS, "join type")
    allowed_fields = {
        "all_of": {"type", "optional_source_nodes"},
        "quorum": {"type", "required_successes"},
    }[kind]
    unknown = sorted(set(value) - allowed_fields)
    if unknown:
        raise CollaborationProfileError(
            f"edge {edge_id} join has unsupported fields: {', '.join(unknown)}"
        )
    optional = _optional_source_nodes(
        value.get("optional_source_nodes", []),
        source_nodes,
        edge_id,
    )
    if kind == "all_of":
        required_successes = len(source_nodes) - len(optional)
        if required_successes < 1:
            raise CollaborationProfileError(
                f"edge {edge_id} all_of join requires one non-optional source"
            )
    else:
        required_successes = value.get("required_successes")
        if (
            type(required_successes) is not int
            or not 1 <= required_successes <= len(source_nodes)
        ):
            raise CollaborationProfileError(
                f"edge {edge_id} quorum required_successes is invalid"
            )
    return {
        "join": kind,
        "required_successes": required_successes,
        "optional_source_nodes": optional,
    }


def _optional_source_nodes(
    value: object,
    source_nodes: list[str],
    edge_id: str,
) -> list[str]:
    if not isinstance(value, list):
        raise CollaborationProfileError(
            f"edge {edge_id} optional_source_nodes must be a list"
        )
    optional = [_safe_id(item, "optional_source_node") for item in value]
    if len(set(optional)) != len(optional) or any(
        node_id not in source_nodes for node_id in optional
    ):
        raise CollaborationProfileError(
            f"edge {edge_id} optional_source_nodes are invalid"
        )
    return optional


def _normalize_proof(
    value: object,
    node_ids: set[str],
    edge_id: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise CollaborationProfileError(
            f"edge {edge_id} proof_requirements must be non-empty"
        )
    proof: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise CollaborationProfileError(
                f"edge {edge_id} proof requirement must be an object"
            )
        kind = _enum(raw.get("kind"), PROOF_KINDS, "proof kind")
        node_id = str(raw.get("node_id") or "").strip()
        if kind in {"terminal_success", "artifact_present", "advisory_ready", "gate_passed"}:
            if node_id not in node_ids:
                raise CollaborationProfileError(
                    f"edge {edge_id} proof {kind} has unknown node_id"
                )
        elif node_id:
            raise CollaborationProfileError(
                f"edge {edge_id} proof {kind} must not define node_id"
            )
        row = {"kind": kind}
        if node_id:
            row["node_id"] = node_id
        proof.append(row)
    return proof


def _validate_cycles(
    nodes: list[dict[str, str]],
    edges: list[dict[str, Any]],
    primitives: list[str],
    feedback: object,
) -> None:
    adjacency = {node["node_id"]: [] for node in nodes}
    for edge in edges:
        for source in edge["source_nodes"]:
            adjacency[source].append(edge["target_node"])
    cycle_nodes = _cycle_nodes(adjacency)
    if not cycle_nodes:
        return
    feedback_data = _normalize_feedback(feedback, nodes)
    allowed = (
        "advisory_feedback" in primitives
        and feedback_data["max_rounds"] > 0
        and feedback_data["requires_changed_snapshot"]
        and feedback_data["adoption_node"] in cycle_nodes
    )
    if not allowed:
        raise CollaborationProfileError(
            "collaboration profile cycle requires bounded advisory_feedback"
        )


def _cycle_nodes(adjacency: dict[str, list[str]]) -> set[str]:
    visited: set[str] = set()
    active: set[str] = set()
    found: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in active:
            found.update(active)
            return
        if node_id in visited:
            return
        visited.add(node_id)
        active.add(node_id)
        for target in adjacency.get(node_id, []):
            visit(target)
        active.remove(node_id)

    for node_id in adjacency:
        visit(node_id)
    return found


def _normalize_feedback(value: object, nodes: list[dict[str, str]]) -> dict[str, Any]:
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict):
        raise CollaborationProfileError("bounded_feedback must be an object")
    max_rounds = value.get("max_rounds")
    if type(max_rounds) is not int or not 1 <= max_rounds <= 16:
        raise CollaborationProfileError("bounded_feedback max_rounds is invalid")
    if value.get("requires_changed_snapshot") is not True:
        raise CollaborationProfileError(
            "bounded_feedback requires_changed_snapshot must be true"
        )
    adoption_node = _safe_id(value.get("adoption_node"), "bounded_feedback adoption_node")
    node_kinds = {node["node_id"]: node["kind"] for node in nodes}
    if node_kinds.get(adoption_node) != "adoption_decision":
        raise CollaborationProfileError(
            "bounded_feedback adoption_node must be an adoption_decision node"
        )
    return {
        "max_rounds": max_rounds,
        "requires_changed_snapshot": True,
        "adoption_node": adoption_node,
    }


def _safe_id(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not SAFE_ID.fullmatch(text):
        raise CollaborationProfileError(f"collaboration profile {field} is invalid")
    return text


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise CollaborationProfileError(f"collaboration profile {field} must be a list")
    normalized = [_safe_id(item, field) for item in value]
    return normalized


def _enum(value: object, allowed: set[str], field: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise CollaborationProfileError(f"collaboration profile {field} is invalid")
    return text
