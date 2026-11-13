from __future__ import annotations

import json
import os
import stat
from pathlib import Path


_METADATA_BYTE_LIMIT = 1024 * 1024
_TASK_EVIDENCE_ENTRY_LIMIT = 4096
_TASK_EVIDENCE_DIRECTORY_LIMIT = 1024


def default_task_evidence_budget() -> dict[str, object]:
    return {
        "schema_version": 1,
        "scope": "task_attempt_directory",
        "accounting": "recursive_regular_file_st_size_no_symlinks",
        "warning_bytes": 12 * 1024 * 1024,
        "hard_limit_bytes": 16 * 1024 * 1024,
        "terminal_reserve_bytes": 64 * 1024,
        "enforcement": "terminal_acceptance",
    }


def _regular_file_bytes(root_fd: int) -> tuple[int, int, int]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    root_fd = os.dup(root_fd)
    total = 0
    entries_seen = 0
    directories_seen = 1
    accounted: list[
        tuple[tuple[tuple[str, int, int], ...], str, int, int, int]
    ] = []
    # ponytail: reopen paths to bound FDs; batch descriptors only if deep-tree
    # scan cost becomes measurable.
    pending: list[tuple[tuple[str, int, int], ...]] = [()]

    def open_directory(
        chain: tuple[tuple[str, int, int], ...],
    ) -> int:
        directory_fd = os.dup(root_fd)
        try:
            for name, expected_device, expected_inode in chain:
                child_fd = os.open(
                    name,
                    directory_flags,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
                opened = os.fstat(directory_fd)
                if (
                    opened.st_dev != expected_device
                    or opened.st_ino != expected_inode
                ):
                    raise OSError(
                        f"directory entry changed while scanning: {name}"
                    )
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    try:
        while pending:
            chain = pending.pop()
            directory_fd = open_directory(chain)
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > _TASK_EVIDENCE_ENTRY_LIMIT:
                            raise OSError(
                                "task evidence entry limit exceeded"
                            )
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode):
                            continue
                        if stat.S_ISDIR(metadata.st_mode):
                            directories_seen += 1
                            if (
                                directories_seen
                                > _TASK_EVIDENCE_DIRECTORY_LIMIT
                            ):
                                raise OSError(
                                    "task evidence directory limit exceeded"
                                )
                            pending.append(
                                chain
                                + (
                                    (
                                        entry.name,
                                        metadata.st_dev,
                                        metadata.st_ino,
                                    ),
                                )
                            )
                        elif stat.S_ISREG(metadata.st_mode):
                            file_fd = os.open(
                                entry.name,
                                file_flags,
                                dir_fd=directory_fd,
                            )
                            try:
                                opened = os.fstat(file_fd)
                                if (
                                    not stat.S_ISREG(opened.st_mode)
                                    or opened.st_dev != metadata.st_dev
                                    or opened.st_ino != metadata.st_ino
                                    or opened.st_size != metadata.st_size
                                ):
                                    raise OSError(
                                        "directory entry changed while "
                                        f"scanning: {entry.name}"
                                    )
                                stable = os.fstat(file_fd)
                                current = os.stat(
                                    entry.name,
                                    dir_fd=directory_fd,
                                    follow_symlinks=False,
                                )
                                if (
                                    not stat.S_ISREG(stable.st_mode)
                                    or stable.st_dev != opened.st_dev
                                    or stable.st_ino != opened.st_ino
                                    or stable.st_size != opened.st_size
                                    or not stat.S_ISREG(current.st_mode)
                                    or current.st_dev != opened.st_dev
                                    or current.st_ino != opened.st_ino
                                    or current.st_size != opened.st_size
                                ):
                                    raise OSError(
                                        "file changed while scanning: "
                                        f"{entry.name}"
                                    )
                                total += opened.st_size
                                accounted.append(
                                    (
                                        chain,
                                        entry.name,
                                        opened.st_dev,
                                        opened.st_ino,
                                        opened.st_size,
                                    )
                                )
                            finally:
                                os.close(file_fd)
            finally:
                os.close(directory_fd)
        for chain, name, device, inode, size in accounted:
            directory_fd = open_directory(chain)
            try:
                file_fd = os.open(
                    name,
                    file_flags,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(file_fd)
                    current = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    stable = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_dev != device
                        or opened.st_ino != inode
                        or opened.st_size != size
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_dev != device
                        or current.st_ino != inode
                        or current.st_size != size
                        or not stat.S_ISREG(stable.st_mode)
                        or stable.st_dev != opened.st_dev
                        or stable.st_ino != opened.st_ino
                        or stable.st_size != opened.st_size
                    ):
                        raise OSError(
                            f"file changed after accounting: {name}"
                        )
                finally:
                    os.close(file_fd)
            finally:
                os.close(directory_fd)
        return total, entries_seen, directories_seen
    finally:
        os.close(root_fd)


def _projection(
    *,
    configured: bool,
    used_bytes: int | None,
    warning_bytes: int | None,
    hard_limit_bytes: int | None,
    terminal_reserve_bytes: int | None,
    remaining_bytes: int | None,
    state: str,
    entry_count: int | None = None,
    directory_count: int | None = None,
) -> dict[str, object]:
    return {
        "configured": configured,
        "used_bytes": used_bytes,
        "warning_bytes": warning_bytes,
        "hard_limit_bytes": hard_limit_bytes,
        "terminal_reserve_bytes": terminal_reserve_bytes,
        "remaining_bytes": remaining_bytes,
        "entry_count": entry_count,
        "entry_limit": _TASK_EVIDENCE_ENTRY_LIMIT if configured else None,
        "directory_count": directory_count,
        "directory_limit": _TASK_EVIDENCE_DIRECTORY_LIMIT if configured else None,
        "state": state,
    }


def _scan_failed(error: Exception) -> dict[str, object]:
    status = _projection(
        configured=True,
        used_bytes=None,
        warning_bytes=None,
        hard_limit_bytes=None,
        terminal_reserve_bytes=None,
        remaining_bytes=None,
        state="scan_failed",
    )
    status["errors"] = [str(error)]
    return status


def _validated_budget(value: object) -> tuple[int, int, int]:
    if not isinstance(value, dict):
        raise ValueError("evidence_budget must be an object")

    expected = default_task_evidence_budget()
    if type(value.get("schema_version")) is not int:
        raise ValueError("invalid evidence_budget schema_version")
    for key in ("warning_bytes", "hard_limit_bytes", "terminal_reserve_bytes"):
        if type(value.get(key)) is not int:
            raise ValueError(f"invalid evidence_budget {key}")
    if value != expected:
        raise ValueError("evidence_budget differs from approved defaults")
    warning_bytes = int(expected["warning_bytes"])
    hard_limit_bytes = int(expected["hard_limit_bytes"])
    terminal_reserve_bytes = int(expected["terminal_reserve_bytes"])
    return warning_bytes, hard_limit_bytes, terminal_reserve_bytes


def _read_json_no_follow(
    path: Path | str,
    *,
    dir_fd: int | None = None,
) -> object:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=dir_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{path} must be a regular file")
        if metadata.st_size > _METADATA_BYTE_LIMIT:
            raise ValueError(
                f"{path} exceeds {_METADATA_BYTE_LIMIT}-byte limit"
            )
        remaining = _METADATA_BYTE_LIMIT + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > _METADATA_BYTE_LIMIT:
        raise ValueError(f"{path} exceeds {_METADATA_BYTE_LIMIT}-byte limit")
    return json.loads(payload.decode("utf-8"))


def _trusted_contract_path(task_dir: Path) -> Path:
    return (
        task_dir.parent.parent
        / "task-contracts"
        / f"{task_dir.name}.evidence-budget.json"
    )


def _task_evidence_budget_status(
    task_dir: Path,
    task_fd: int,
) -> dict[str, object]:
    missing = object()
    try:
        trusted = _read_json_no_follow(_trusted_contract_path(task_dir))
    except FileNotFoundError:
        trusted = missing
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return _scan_failed(error)

    try:
        metadata = _read_json_no_follow("task.json", dir_fd=task_fd)
    except FileNotFoundError:
        metadata = None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return _scan_failed(error)

    try:
        if trusted is missing:
            if metadata is None:
                return _projection(
                    configured=False,
                    used_bytes=None,
                    warning_bytes=None,
                    hard_limit_bytes=None,
                    terminal_reserve_bytes=None,
                    remaining_bytes=None,
                    state="legacy_unconfigured",
                )
            if not isinstance(metadata, dict):
                raise ValueError("task.json must contain an object")
            if "evidence_budget" in metadata:
                raise ValueError("trusted evidence budget contract is missing")
            return _projection(
                configured=False,
                used_bytes=None,
                warning_bytes=None,
                hard_limit_bytes=None,
                terminal_reserve_bytes=None,
                remaining_bytes=None,
                state="legacy_unconfigured",
            )

        if not isinstance(trusted, dict):
            raise ValueError("trusted evidence budget contract must be an object")
        expected_contract = {
            "schema_version": 1,
            "contract_kind": "task_evidence_budget",
            "task_id": task_dir.name,
            "evidence_budget": default_task_evidence_budget(),
        }
        if type(trusted.get("schema_version")) is not int:
            raise ValueError("invalid trusted contract schema_version")
        if trusted != expected_contract:
            raise ValueError("invalid trusted evidence budget contract")
        if not isinstance(metadata, dict):
            raise ValueError("task.json must contain an object")
        if "evidence_budget" not in metadata:
            raise ValueError("task.json evidence_budget is missing")
        _validated_budget(metadata["evidence_budget"])
        if metadata["evidence_budget"] != trusted["evidence_budget"]:
            raise ValueError(
                "task.json evidence_budget differs from trusted contract"
            )
        warning_bytes, hard_limit_bytes, terminal_reserve_bytes = (
            _validated_budget(trusted["evidence_budget"])
        )
        used_bytes, entry_count, directory_count = _regular_file_bytes(task_fd)
    except (OSError, ValueError) as error:
        return _scan_failed(error)

    if used_bytes + terminal_reserve_bytes > hard_limit_bytes:
        state = "exceeded"
    elif used_bytes >= warning_bytes:
        state = "warning"
    else:
        state = "ok"

    return _projection(
        configured=True,
        used_bytes=used_bytes,
        warning_bytes=warning_bytes,
        hard_limit_bytes=hard_limit_bytes,
        terminal_reserve_bytes=terminal_reserve_bytes,
        remaining_bytes=max(hard_limit_bytes - used_bytes, 0),
        state=state,
        entry_count=entry_count,
        directory_count=directory_count,
    )


def task_evidence_budget_status(task_dir: Path) -> dict[str, object]:
    try:
        task_fd = os.open(
            task_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        return _scan_failed(error)
    try:
        return _task_evidence_budget_status(task_dir, task_fd)
    finally:
        os.close(task_fd)
