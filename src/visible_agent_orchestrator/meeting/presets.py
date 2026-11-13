"""Built-in meeting presets for AG2-style advisory runs."""

from __future__ import annotations

from dataclasses import dataclass

from .builtins import BUILTIN_TERMINATION_CONDITIONS
from .models import MeetingParticipant, MeetingSpec
from .runner import SUPPORTED_POLICIES


@dataclass(frozen=True)
class MeetingPreset:
    name: str
    participants: tuple[MeetingParticipant, ...]
    max_rounds: int
    policy: str = "role_order"
    termination_condition: str = "all_participants_spoke"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("preset name must be non-empty")
        if not self.participants:
            raise ValueError("preset participants must be non-empty")
        if self.max_rounds < 1:
            raise ValueError("preset max_rounds must be positive")
        if self.policy not in SUPPORTED_POLICIES:
            raise ValueError(f"unsupported preset policy: {self.policy}")
        if self.termination_condition not in BUILTIN_TERMINATION_CONDITIONS:
            raise ValueError(f"unsupported preset termination_condition: {self.termination_condition}")

        seen_names: set[str] = set()
        for participant in self.participants:
            if not participant.name.strip():
                raise ValueError("participant name must be non-empty")
            if not participant.role.strip():
                raise ValueError("participant role must be non-empty")
            if not participant.goal.strip():
                raise ValueError("participant goal must be non-empty")
            if participant.name in seen_names:
                raise ValueError(f"duplicate participant name: {participant.name}")
            seen_names.add(participant.name)

    def to_spec(self, *, task: str, max_rounds: int | None = None) -> MeetingSpec:
        resolved_max_rounds = max_rounds if max_rounds is not None else self.max_rounds
        if resolved_max_rounds < 1 or resolved_max_rounds > self.max_rounds:
            raise ValueError(f"max_rounds must be between 1 and {self.max_rounds} for preset {self.name}")
        return MeetingSpec(
            task=task,
            participants=list(self.participants),
            max_rounds=resolved_max_rounds,
            policy=self.policy,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "participants": [participant.to_dict() for participant in self.participants],
            "max_rounds": self.max_rounds,
            "policy": self.policy,
            "termination_condition": self.termination_condition,
        }


BUILTIN_MEETING_PRESETS: dict[str, MeetingPreset] = {
    "idea-review": MeetingPreset(
        name="idea-review",
        participants=(
            MeetingParticipant(
                name="proposer",
                role="proposer",
                goal="state the hypothesis and strongest implementation path",
            ),
            MeetingParticipant(
                name="skeptic",
                role="skeptic",
                goal="challenge assumptions, risks, and missing evidence",
            ),
            MeetingParticipant(
                name="synthesizer",
                role="synthesizer",
                goal="merge the useful positions into a next-step recommendation",
            ),
        ),
        max_rounds=3,
    ),
    "code-design-review": MeetingPreset(
        name="code-design-review",
        participants=(
            MeetingParticipant(
                name="architect",
                role="architect",
                goal="define the smallest coherent design boundary",
            ),
            MeetingParticipant(
                name="test-engineer",
                role="test-engineer",
                goal="define the RED tests and verification gates",
            ),
            MeetingParticipant(
                name="reviewer",
                role="reviewer",
                goal="identify integration risks and missing safety checks",
            ),
            MeetingParticipant(
                name="implementer",
                role="implementer",
                goal="recommend the implementation order",
            ),
        ),
        max_rounds=4,
    ),
}


def _validate_builtin_preset_registry() -> None:
    for preset_key, preset in BUILTIN_MEETING_PRESETS.items():
        if preset_key != preset.name:
            raise ValueError(f"meeting preset registry key mismatch: {preset_key} != {preset.name}")


_validate_builtin_preset_registry()

MEETING_PRESET_NAMES = tuple(sorted(BUILTIN_MEETING_PRESETS))


def resolve_meeting_preset(name: str) -> MeetingPreset:
    try:
        return BUILTIN_MEETING_PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"unknown meeting preset: {name}") from exc
