"""Meeting-layer runtime adapter seam for AG2-style execution."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import (
    Ag2HostLocalRuntimeConfig,
    MeetingDecision,
    MeetingMessage,
    MeetingResult,
    MeetingSpec,
    validate_ag2_effective_approval_payload,
)
from .runner import MeetingRunner, Selector, TerminationCondition


@dataclass(frozen=True)
class Ag2RuntimeAdapterResult:
    runtime: str
    adapter_kind: str
    result: MeetingResult
    would_execute_live: bool = False
    would_import_ag2: bool = False
    provider_or_model_config_required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "adapter_kind": self.adapter_kind,
            "would_execute_live": self.would_execute_live,
            "would_import_ag2": self.would_import_ag2,
            "provider_or_model_config_required": self.provider_or_model_config_required,
            "metadata": dict(self.metadata),
        }


class MockAg2RuntimeAdapter:
    """Deterministic AG2-style adapter that never imports AG2 or calls providers."""

    runtime = "mock"
    adapter_kind = "mock_ag2_runtime_adapter"

    def __init__(
        self,
        *,
        selector: Selector | None = None,
        termination_condition: TerminationCondition | None = None,
    ) -> None:
        self._runner = MeetingRunner(selector=selector, termination_condition=termination_condition)

    def run(self, spec: MeetingSpec) -> Ag2RuntimeAdapterResult:
        result = self._runner.run(spec)
        return Ag2RuntimeAdapterResult(
            runtime=self.runtime,
            adapter_kind=self.adapter_kind,
            result=result,
            metadata={
                "source": "deterministic_mock",
                "rounds_requested": spec.max_rounds,
                "policy": spec.policy,
            },
        )


ModuleResolver = Callable[[str], object | None]
ModelClient = Callable[[dict[str, Any]], str]


def fake_ag2_model_client(request: dict[str, Any]) -> str:
    return f"{request['role']} model response for: {request['task']}"


class OpenAICompatibleModelClient:
    """Small host-local OpenAI-compatible model client for gated live smoke tests."""

    def __init__(self, runtime_config: Ag2HostLocalRuntimeConfig) -> None:
        self._runtime_config = runtime_config

    def __call__(self, request: dict[str, Any]) -> str:
        api_key = os.environ.get(self._runtime_config.api_key_env, "")
        if not api_key:
            raise ValueError("AG2 runtime API key environment variable is not set")
        endpoint = f"{self._runtime_config.base_url}/chat/completions"
        body = {
            "model": self._runtime_config.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"You are {request['speaker']}, role: {request['role']}.",
                },
                {
                    "role": "user",
                    "content": str(request["task"]),
                },
            ],
            "max_tokens": 512,
            "temperature": 0.2,
        }
        http_request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._runtime_config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ValueError(f"AG2 runtime model request failed: {exc}") from exc
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ValueError("AG2 runtime model response did not include choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AG2 runtime model response content was empty")
        return content.strip()


class OptionalAg2RuntimeAdapter:
    """Fail-closed seam for a future real AG2 adapter.

    The default path does not import AG2. Tests may inject a resolver to prove
    the fake-client path without installing dependencies or calling providers.
    """

    runtime = "real"
    adapter_kind = "real_ag2_optional_adapter"
    import_name = "autogen"

    def __init__(
        self,
        *,
        module_resolver: ModuleResolver | None = None,
        selector: Selector | None = None,
        termination_condition: TerminationCondition | None = None,
    ) -> None:
        self._module_resolver = module_resolver
        self._runner = MeetingRunner(selector=selector, termination_condition=termination_condition)

    def availability(self) -> dict[str, Any]:
        if self._module_resolver is None:
            return self._availability_payload(
                available=False,
                status="ag2_not_installed",
                reason="no optional AG2 module resolver configured",
            )
        try:
            module = self._module_resolver(self.import_name)
        except Exception as exc:  # pragma: no cover - defensive normalization
            return self._availability_payload(
                available=False,
                status="ag2_not_installed",
                reason=str(exc),
            )
        if module is None:
            return self._availability_payload(
                available=False,
                status="ag2_not_installed",
                reason="optional AG2 module resolver returned no module",
            )
        return self._availability_payload(
            available=True,
            status="ag2_available_with_fake_client",
            reason="optional AG2 module resolver supplied a test module",
        )

    def run(self, spec: MeetingSpec) -> Ag2RuntimeAdapterResult:
        status = self.availability()
        if not status["available"]:
            raise ValueError(status["status"])
        result = self._runner.run(spec)
        return Ag2RuntimeAdapterResult(
            runtime=self.runtime,
            adapter_kind=self.adapter_kind,
            result=result,
            metadata={
                "availability_status": status["status"],
                "import_name": self.import_name,
                "source": "optional_fake_client",
            },
        )

    def _availability_payload(self, *, available: bool, status: str, reason: str) -> dict[str, Any]:
        return {
            "available": available,
            "status": status,
            "import_name": self.import_name,
            "reason": reason,
            "runtime_import_allowed": False,
            "would_import_ag2": False,
            "would_execute_live": False,
            "provider_or_model_config_required": False,
        }


class RealAg2RuntimeAdapter:
    """Gated model-driven AG2-style runtime adapter.

    This keeps the meeting artifact schema local while allowing a caller to
    provide either a fake model client for tests or an OpenAI-compatible live
    client for explicitly approved host-local smoke runs.
    """

    runtime = "real"
    adapter_kind = "real_ag2_runtime_adapter"

    def __init__(
        self,
        *,
        approval_validation: dict[str, Any],
        runtime_config: Ag2HostLocalRuntimeConfig,
        model_client: ModelClient | None = None,
        runtime_mode: str = "fake",
        allow_model_call: bool = False,
        selector: Selector | None = None,
        termination_condition: TerminationCondition | None = None,
    ) -> None:
        if runtime_mode not in {"fake", "live"}:
            raise ValueError("AG2 real runtime mode must be fake or live")
        normalized_approval = approval_validation.get("normalized_payload")
        if (
            approval_validation.get("validation_kind")
            != "ag2_effective_approval_payload_validation"
            or not isinstance(normalized_approval, dict)
        ):
            raise ValueError(
                "AG2 real runtime requires effective approval validation "
                "with normalized_payload"
            )
        effective_approval = validate_ag2_effective_approval_payload(
            normalized_approval
        )
        if not effective_approval.get("valid") or not effective_approval.get(
            "approval_effective"
        ):
            raise ValueError("AG2 real runtime requires effective approval validation")
        if not runtime_config.to_redacted_payload()["ready_for_model_client"]:
            raise ValueError("AG2 real runtime requires a complete host-local runtime config")
        if runtime_mode == "live" and not allow_model_call:
            raise ValueError("--allow-real-model-call is required for live AG2 runtime mode")
        self._approval_validation = effective_approval
        self._runtime_config = runtime_config
        self._runtime_mode = runtime_mode
        self._allow_model_call = allow_model_call
        self._selector = selector
        self._termination_condition = termination_condition
        if model_client is not None:
            self._model_client = model_client
        elif runtime_mode == "fake":
            self._model_client = fake_ag2_model_client
        else:
            self._model_client = OpenAICompatibleModelClient(runtime_config)

    def run(self, spec: MeetingSpec) -> Ag2RuntimeAdapterResult:
        base = MeetingRunner(selector=self._selector, termination_condition=self._termination_condition).run(spec)
        messages: list[MeetingMessage] = []
        empty_response_retries_used = 0
        for message in base.messages:
            request = {
                "task": spec.task,
                "speaker": message.speaker,
                "role": message.role,
                "round_index": message.round_index,
                "runtime_mode": self._runtime_mode,
            }
            response, retries_used = self._call_model_with_empty_response_retry(request)
            empty_response_retries_used += retries_used
            messages.append(
                MeetingMessage(
                    round_index=message.round_index,
                    speaker=message.speaker,
                    role=message.role,
                    content=response,
                )
            )
        live_call = self._runtime_mode == "live" and self._allow_model_call
        result = MeetingResult(
            task=base.task,
            participants=base.participants,
            messages=messages,
            selector_decisions=[MeetingDecision(**decision.to_dict()) for decision in base.selector_decisions],
            termination_reason=base.termination_reason,
            verdict=base.verdict,
            proposed_actions=list(base.proposed_actions),
            risks=list(base.risks),
        )
        return Ag2RuntimeAdapterResult(
            runtime=self.runtime,
            adapter_kind=self.adapter_kind,
            result=result,
            would_execute_live=live_call,
            would_import_ag2=False,
            provider_or_model_config_required=live_call,
            metadata={
                "runtime_mode": self._runtime_mode,
                "model_client_calls": len(messages),
                "empty_response_retries_used": empty_response_retries_used,
                "approval_validation_kind": self._approval_validation.get("validation_kind", ""),
                "runtime_config": self._runtime_config.to_redacted_payload(),
            },
        )

    def _call_model_with_empty_response_retry(self, request: dict[str, Any]) -> tuple[str, int]:
        retries_used = 0
        max_retries = self._runtime_config.empty_response_retries
        while True:
            try:
                return self._model_client(request), retries_used
            except ValueError as exc:
                if "response content was empty" not in str(exc) or retries_used >= max_retries:
                    raise
                retries_used += 1
