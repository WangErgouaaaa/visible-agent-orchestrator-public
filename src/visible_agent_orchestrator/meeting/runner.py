"""Deterministic no-live meeting runner."""

from __future__ import annotations

from collections.abc import Callable

from .models import MeetingDecision, MeetingMessage, MeetingResult, MeetingSpec, SpeakerSelectionResult

Selector = Callable[[MeetingSpec, int, str], SpeakerSelectionResult]
TerminationCondition = Callable[
    [MeetingSpec, list[MeetingMessage], list[MeetingDecision]], SpeakerSelectionResult | None
]


def round_robin_selector(spec: MeetingSpec, round_index: int, previous_speaker: str) -> SpeakerSelectionResult:
    participant = spec.participants[round_index % len(spec.participants)]
    reason = "first speaker" if not previous_speaker else f"after {previous_speaker}"
    return SpeakerSelectionResult(next_speaker=participant.name, selection_method="round_robin", reason=reason)


def role_order_selector(spec: MeetingSpec, round_index: int, previous_speaker: str) -> SpeakerSelectionResult:
    if round_index >= len(spec.participants):
        return SpeakerSelectionResult(
            selection_method="role_order",
            terminate=True,
            reason="role_order policy complete",
            termination_type="role_order",
        )
    participant = spec.participants[round_index]
    reason = "first role" if not previous_speaker else f"after {previous_speaker}"
    return SpeakerSelectionResult(next_speaker=participant.name, selection_method="role_order", reason=reason)


BUILTIN_SELECTOR_POLICIES: dict[str, Selector] = {
    "role_order": role_order_selector,
    "round_robin": round_robin_selector,
}
SUPPORTED_POLICIES = set(BUILTIN_SELECTOR_POLICIES)


def resolve_builtin_selector_policy(name: str) -> Selector:
    try:
        return BUILTIN_SELECTOR_POLICIES[name]
    except KeyError as exc:
        raise ValueError(f"unknown built-in selector policy: {name}") from exc


class MeetingRunner:
    def __init__(
        self,
        selector: Selector | None = None,
        termination_condition: TerminationCondition | None = None,
    ) -> None:
        self._selector = selector
        self._termination_condition = termination_condition

    def run(self, spec: MeetingSpec) -> MeetingResult:
        if not spec.participants:
            raise ValueError("meeting requires at least one participant")
        if spec.max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if spec.policy not in SUPPORTED_POLICIES:
            raise ValueError(f"unsupported meeting policy: {spec.policy}")
        selector = self._selector or resolve_builtin_selector_policy(spec.policy)

        messages: list[MeetingMessage] = []
        decisions: list[MeetingDecision] = []
        previous_speaker = ""
        termination_reason = "max_rounds"
        verdict = "needs_review"

        for round_index in range(spec.max_rounds):
            selected = selector(spec, round_index, previous_speaker)
            decision = MeetingDecision(
                round_index=round_index,
                previous_speaker=previous_speaker,
                next_speaker=selected.next_speaker,
                selection_method=selected.selection_method or spec.policy,
                terminate=selected.terminate,
                reason=selected.reason,
                termination_type=selected.termination_type,
            )
            decisions.append(decision)
            if selected.terminate:
                termination_reason = selected.reason or "terminated"
                verdict = "terminated"
                break

            participant = self._participant_by_name(spec, selected.next_speaker)
            messages.append(
                MeetingMessage(
                    round_index=round_index,
                    speaker=participant.name,
                    role=participant.role,
                    content=f"{participant.role} response for: {spec.task}",
                )
            )
            previous_speaker = participant.name
            if self._termination_condition is not None:
                termination = self._termination_condition(spec, list(messages), list(decisions))
                if termination is not None and termination.terminate:
                    decisions.append(
                        MeetingDecision(
                            round_index=round_index,
                            previous_speaker=participant.name,
                            next_speaker="",
                            selection_method=termination.selection_method or "termination_condition",
                            terminate=True,
                            reason=termination.reason,
                            termination_type=termination.termination_type,
                        )
                    )
                    termination_reason = termination.reason or "terminated"
                    verdict = "terminated"
                    break

        return MeetingResult(
            task=spec.task,
            participants=list(spec.participants),
            messages=messages,
            selector_decisions=decisions,
            termination_reason=termination_reason,
            verdict=verdict,
        )

    @staticmethod
    def _participant_by_name(spec: MeetingSpec, name: str):
        for participant in spec.participants:
            if participant.name == name:
                return participant
        raise ValueError(f"unknown meeting participant: {name}")
