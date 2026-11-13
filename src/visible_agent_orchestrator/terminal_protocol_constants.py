from __future__ import annotations

from typing import Final


TASK_PROTOCOL_VERSION: Final = 2
TERMINAL_PROTOCOL_VERSION: Final = TASK_PROTOCOL_VERSION
TERMINAL_PROTOCOL_CLASSIFICATION_LEGACY: Final = "legacy"
TERMINAL_PROTOCOL_CLASSIFICATION_MODERN: Final = "modern_v2"
TERMINAL_PROTOCOL_CLASSIFICATION_INVALID: Final = "invalid"
OPERATOR_RECOVERY_CLAIM_KIND: Final = "visible_operator_recovery_closeout"
TERMINAL_OUTPUT_CLAIM_KIND: Final = "visible_terminal_output"
CREATION_RECORD_KIND: Final = "terminal_protocol_creation"
FENCE_RECORD_KIND: Final = "visible_terminal_output_acceptance"
LEGACY_BASELINE_KIND: Final = "legacy_terminal_identity_baseline"
LEGACY_BASELINE_SCHEMA_VERSION: Final = 1
LEGACY_BASELINE_FILENAME: Final = "legacy_terminal_baseline.json"
LEGACY_ACTIVATION_BATCH_FILENAME: Final = "legacy_activation_batch.json"
CREATION_RECORD_SUFFIX: Final = ".terminal-protocol.json"
IDENTITY_FIELDS: Final = (
    "task_protocol_version",
    "terminal_acceptance_required",
    "run_id",
    "task_id",
    "attempt_id",
    "contract_hash",
    "worker_id",
    "session_id",
    "instance_id",
    "binding_generation",
    "fencing_generation",
)
PROTOCOL_ALIASES: Final = ("task_protocol_version", "terminal_protocol_version")
CREATION_IDENTITY_FIELDS: Final = frozenset(
    (
        "task_protocol_version",
        "terminal_acceptance_required",
        "run_id",
        "task_id",
        "attempt_id",
        "contract_hash",
    )
)
CURRENT_BINDING_FIELDS: Final = frozenset(
    (
        "worker_id",
        "session_id",
        "instance_id",
        "binding_generation",
        "fencing_generation",
    )
)
MODERN_RECORDS: Final = (
    "task.json",
    "dispatch.json",
    "acceptance_fence.json",
    "terminal_output_claim.json",
)
REQUIRED_MODERN_RECORDS: Final = (
    "task.json",
    "dispatch.json",
    "acceptance_fence.json",
)
