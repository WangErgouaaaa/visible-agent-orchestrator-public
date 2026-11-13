"""Runtime adapter exceptions."""

from __future__ import annotations


class RuntimeAdapterError(Exception):
    """Base class for expected runtime adapter failures."""


class RegistryError(RuntimeAdapterError):
    """Visible worker registry is missing, invalid, or unsafe."""


class WorkerNotFoundError(RegistryError):
    """Requested worker id is absent from the visible worker registry."""


class UnsupportedRuntimeOperation(RuntimeAdapterError):
    """The selected runtime adapter intentionally refuses this operation."""
