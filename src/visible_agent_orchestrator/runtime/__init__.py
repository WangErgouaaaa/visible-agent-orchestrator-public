"""Runtime substrate adapter package."""

from .registry import (
    is_auto_submit_allowed,
    load_visible_workers,
    resolve_default_worker,
    resolve_worker_record,
    validate_worker_for_dispatch,
)
from .models import SubmitCapability, SubmitPlan
from .rmux_spike import RmuxSpikeAdapter
from .tmux_reference import TmuxReferenceAdapter

__all__ = [
    "RmuxSpikeAdapter",
    "SubmitCapability",
    "SubmitPlan",
    "TmuxReferenceAdapter",
    "is_auto_submit_allowed",
    "load_visible_workers",
    "resolve_default_worker",
    "resolve_worker_record",
    "validate_worker_for_dispatch",
]
