from visible_agent_orchestrator.prompt_budget import (
    DEFAULT_MAX_PROMPT_BYTES,
    resolve_context_budget_policy,
)


def test_unknown_provider_uses_documented_default(monkeypatch):
    monkeypatch.delenv("OMX_VISIBLE_WORKER_MAX_PROMPT_BYTES", raising=False)
    policy = resolve_context_budget_policy(model="example-provider/example-model")
    assert policy["max_prompt_bytes"] == DEFAULT_MAX_PROMPT_BYTES
    assert policy["effective_context_tokens"] == 0
    assert policy["effective_limit_source"] == "default_max_prompt_bytes"


def test_explicit_context_limit_is_capped_by_explicit_byte_limit():
    policy = resolve_context_budget_policy(
        effective_context_tokens=10_000,
        recommended_prune_trigger_ratio=0.5,
        max_prompt_bytes=12_000,
    )
    assert policy["compaction_trigger_tokens"] == 5_000
    assert policy["max_prompt_bytes"] == 12_000
