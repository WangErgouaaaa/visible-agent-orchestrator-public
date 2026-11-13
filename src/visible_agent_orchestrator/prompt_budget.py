"""Prompt-size budget helpers for visible worker dispatch."""

from __future__ import annotations

import hashlib
import os
from typing import Any


DEFAULT_MAX_PROMPT_BYTES = 48000
DEFAULT_VISIBLE_PROMPT_OVERHEAD_BYTES = 7000
DEFAULT_PRUNE_TRIGGER_RATIO = 0.65
DEFAULT_ESTIMATED_BYTES_PER_TOKEN = 4
ENV_MAX_PROMPT_BYTES = "OMX_VISIBLE_WORKER_MAX_PROMPT_BYTES"


def resolve_max_prompt_bytes(value: Any = None) -> int:
    if value not in (None, ""):
        return _positive_int(value, DEFAULT_MAX_PROMPT_BYTES)
    env_value = os.environ.get(ENV_MAX_PROMPT_BYTES, "")
    if env_value.strip():
        return _positive_int(env_value, DEFAULT_MAX_PROMPT_BYTES)
    return DEFAULT_MAX_PROMPT_BYTES


def build_prompt_budget(
    *,
    title: str,
    body: str,
    model: str = "",
    worker_id: str = "",
    lane_id: str = "",
    max_prompt_bytes: Any = None,
    nominal_context_tokens: Any = None,
    effective_context_tokens: Any = None,
    recommended_prune_trigger_ratio: Any = DEFAULT_PRUNE_TRIGGER_RATIO,
    estimated_bytes_per_token: Any = DEFAULT_ESTIMATED_BYTES_PER_TOKEN,
    overhead_bytes: int = DEFAULT_VISIBLE_PROMPT_OVERHEAD_BYTES,
) -> dict[str, Any]:
    title_text = str(title or "")
    body_text = str(body or "")
    title_bytes = _byte_count(title_text)
    body_bytes = _byte_count(body_text)
    overhead = max(0, int(overhead_bytes))
    context_policy = resolve_context_budget_policy(
        model=model,
        max_prompt_bytes=max_prompt_bytes,
        nominal_context_tokens=nominal_context_tokens,
        effective_context_tokens=effective_context_tokens,
        recommended_prune_trigger_ratio=recommended_prune_trigger_ratio,
        estimated_bytes_per_token=estimated_bytes_per_token,
    )
    limit = int(context_policy["max_prompt_bytes"])
    estimate = title_bytes + body_bytes + overhead
    over_by = max(0, estimate - limit)
    status = "ok" if over_by == 0 else "rejected"
    budget = {
        "status": status,
        "model": str(model or ""),
        "worker_id": str(worker_id or ""),
        "lane_id": str(lane_id or ""),
        "title_bytes": title_bytes,
        "body_bytes": body_bytes,
        "prompt_bytes_estimate": estimate,
        "prompt_overhead_bytes": overhead,
        "max_prompt_bytes": limit,
        "over_budget_by_bytes": over_by,
        "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "source": "visible_worker_prompt_budget",
    }
    budget.update(context_policy)
    return budget


def prompt_budget_error(worker_id: str, budget: dict[str, Any]) -> str:
    return (
        f"{worker_id} prompt budget rejected: "
        f"estimate={budget.get('prompt_bytes_estimate')} "
        f"max={budget.get('max_prompt_bytes')} "
        f"over_by={budget.get('over_budget_by_bytes')}"
    )


def _byte_count(value: str) -> int:
    return len(str(value or "").encode("utf-8"))


def resolve_context_budget_policy(
    *,
    model: str = "",
    max_prompt_bytes: Any = None,
    nominal_context_tokens: Any = None,
    effective_context_tokens: Any = None,
    recommended_prune_trigger_ratio: Any = DEFAULT_PRUNE_TRIGGER_RATIO,
    estimated_bytes_per_token: Any = DEFAULT_ESTIMATED_BYTES_PER_TOKEN,
) -> dict[str, Any]:
    nominal_tokens = _positive_int(nominal_context_tokens, 0)
    effective_tokens = _positive_int(effective_context_tokens, 0)
    ratio = _bounded_ratio(recommended_prune_trigger_ratio, DEFAULT_PRUNE_TRIGGER_RATIO)
    bytes_per_token = _positive_int(estimated_bytes_per_token, DEFAULT_ESTIMATED_BYTES_PER_TOKEN)
    explicit_byte_limit = max_prompt_bytes not in (None, "")
    env_byte_limit = bool(os.environ.get(ENV_MAX_PROMPT_BYTES, "").strip())
    byte_limit = resolve_max_prompt_bytes(max_prompt_bytes)

    effective_limit_source = "default_max_prompt_bytes"
    if effective_tokens > 0:
        effective_limit_source = "explicit_effective_context_tokens"
    elif nominal_tokens > 0:
        effective_tokens = nominal_tokens
        effective_limit_source = "nominal_context_tokens"

    compaction_trigger_tokens = 0
    policy_byte_limit = 0
    if effective_tokens > 0:
        compaction_trigger_tokens = max(1, int(effective_tokens * ratio))
        policy_byte_limit = max(1, compaction_trigger_tokens * bytes_per_token)
        if explicit_byte_limit or env_byte_limit:
            byte_limit = min(byte_limit, policy_byte_limit)
            effective_limit_source = f"{effective_limit_source}_capped_by_max_prompt_bytes"
        else:
            byte_limit = policy_byte_limit

    max_prompt_tokens = max(1, byte_limit // bytes_per_token)
    return {
        "nominal_context_tokens": nominal_tokens,
        "effective_context_tokens": effective_tokens,
        "effective_limit_source": effective_limit_source,
        "recommended_prune_trigger_ratio": ratio,
        "estimated_bytes_per_token": bytes_per_token,
        "compaction_trigger_tokens": compaction_trigger_tokens,
        "max_prompt_tokens": max_prompt_tokens,
        "max_prompt_bytes": byte_limit,
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bounded_ratio(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(parsed, 1.0)
