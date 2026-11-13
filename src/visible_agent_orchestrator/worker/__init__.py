"""Worker agent backend abstractions.

Runtime substrate backends answer where a process lives. Worker backends answer
which agent family owns task receipt semantics. Keep those concerns separate.
"""

from .backends import available_backends, default_backend_name, get_backend
from .live_enable import LiveEnableConfig, LiveEnableDecision, evaluate_live_enable
from .preflight import (
    DryRunPreflightEvidence,
    DryRunPreflightRequest,
    collect_preflight_evidence_from_fixture,
    evaluate_preflight_evidence,
)
from .registration import DryRunRegistrationPlan, DryRunRegistrationRequest, build_dry_run_registration_plan

__all__ = [
    "DryRunPreflightEvidence",
    "DryRunPreflightRequest",
    "DryRunRegistrationPlan",
    "DryRunRegistrationRequest",
    "LiveEnableConfig",
    "LiveEnableDecision",
    "available_backends",
    "build_dry_run_registration_plan",
    "collect_preflight_evidence_from_fixture",
    "default_backend_name",
    "evaluate_live_enable",
    "evaluate_preflight_evidence",
    "get_backend",
]
