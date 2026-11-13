"""Built-in no-live termination conditions for meeting prototypes."""

from __future__ import annotations

from .models import MeetingDecision, MeetingMessage, MeetingSpec, SpeakerSelectionResult
from .runner import TerminationCondition


def single_turn_termination_condition(
    spec: MeetingSpec,
    messages: list[MeetingMessage],
    decisions: list[MeetingDecision],
) -> SpeakerSelectionResult | None:
    if not messages:
        return None
    return SpeakerSelectionResult(
        terminate=True,
        reason="single_turn condition met",
        termination_type="single_turn",
    )


def all_participants_spoke_termination_condition(
    spec: MeetingSpec,
    messages: list[MeetingMessage],
    decisions: list[MeetingDecision],
) -> SpeakerSelectionResult | None:
    participant_names = {participant.name for participant in spec.participants}
    speakers = {message.speaker for message in messages}
    if participant_names <= speakers:
        return SpeakerSelectionResult(
            terminate=True,
            reason="all_participants_spoke condition met",
            termination_type="all_participants_spoke",
        )
    return None


BUILTIN_TERMINATION_CONDITIONS: dict[str, TerminationCondition] = {
    "all_participants_spoke": all_participants_spoke_termination_condition,
    "single_turn": single_turn_termination_condition,
}


def resolve_builtin_termination_condition(name: str) -> TerminationCondition:
    try:
        return BUILTIN_TERMINATION_CONDITIONS[name]
    except KeyError as exc:
        raise ValueError(f"unknown built-in termination condition: {name}") from exc
