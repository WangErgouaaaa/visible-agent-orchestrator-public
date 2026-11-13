"""Cross-process gate between shared runtime mutation and worker dispatch."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path


def global_mutation_gate_path(root: Path) -> Path:
    return Path(root).resolve() / ".omx" / "framework" / "team" / ".global-mutation.lock"


@contextmanager
def global_mutation_gate(root: Path, *, exclusive: bool):
    path = global_mutation_gate_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
