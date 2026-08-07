"""Portable artifact identities and operation-owned Course workspace access.

Artifact paths are stored as canonical POSIX-relative strings. Filesystem
sinks use the functions in this module so validation and access happen in one
operation. Existing symlink or reparse-point components are rejected, even
when they currently resolve back inside the workspace. This keeps immutable
artifact identity independent from mutable link targets.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO, Iterator

from deeptutor.services.path_service import PathService

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_SECURE_DIR_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
)


class InvalidArtifactPathError(ValueError):
    pass


def normalize_artifact_relative_path(relative_path: str) -> str:
    """Validate and return an already-canonical POSIX-relative artifact path."""
    if not isinstance(relative_path, str) or not relative_path:
        raise InvalidArtifactPathError("Artifact path must not be empty")
    if "\x00" in relative_path:
        raise InvalidArtifactPathError("Artifact path must not contain NUL")
    if "\\" in relative_path:
        raise InvalidArtifactPathError("Artifact path must use POSIX separators")
    if relative_path.startswith("/") or _WINDOWS_DRIVE_PREFIX.match(relative_path):
        raise InvalidArtifactPathError("Artifact path must be relative")

    components = relative_path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise InvalidArtifactPathError("Artifact path contains a non-canonical component")
    return "/".join(components)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _validate_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InvalidArtifactPathError("Course storage directory is unavailable") from exc
    if _is_link_or_reparse(metadata):
        raise InvalidArtifactPathError("Course storage contains a symlink or reparse point")
    if not stat.S_ISDIR(metadata.st_mode):
        raise InvalidArtifactPathError("Course storage boundary is not a directory")


def _supports_secure_dir_fd() -> bool:
    return _SECURE_DIR_FD_SUPPORTED


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _prepare_trusted_anchor(path_service: PathService) -> Path:
    """Create the configured root, then reject links at that trusted boundary."""
    anchor = path_service.workspace_root
    anchor.mkdir(parents=True, exist_ok=True)
    _validate_directory(anchor)
    if path_service.get_user_root() != anchor / "user":
        raise InvalidArtifactPathError("Unexpected Course storage layout")
    return anchor


def _ensure_directory_chain(path_service: PathService, components: tuple[str, ...]) -> bool:
    anchor = _prepare_trusted_anchor(path_service)
    if not _supports_secure_dir_fd():
        current = anchor
        leaf_created = False
        for index, component in enumerate(components):
            current = current / component
            try:
                current.mkdir()
                created = True
            except FileExistsError:
                created = False
            _validate_directory(current)
            if index == len(components) - 1:
                leaf_created = created
        return leaf_created

    current_fd = os.open(anchor, _directory_flags())
    leaf_created = False
    try:
        for index, component in enumerate(components):
            try:
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
                created = True
            except FileExistsError:
                created = False
            try:
                child_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise InvalidArtifactPathError(
                    "Course storage contains a symlink or reparse point"
                ) from exc
            os.close(current_fd)
            current_fd = child_fd
            if index == len(components) - 1:
                leaf_created = created
    finally:
        os.close(current_fd)
    return leaf_created


def ensure_course_data_root(path_service: PathService) -> None:
    """Ensure the per-user Course data root without following its final link."""
    _ensure_directory_chain(path_service, ("user",))


def ensure_course_workspace(path_service: PathService, course_id: str) -> bool:
    """Atomically ensure one Course workspace; return whether its leaf was created."""
    path_service.get_course_workspace(course_id)  # Canonical UUID validation.
    return _ensure_directory_chain(
        path_service,
        ("user", "workspace", "course-mode", "courses", course_id),
    )


def remove_empty_course_workspace(path_service: PathService, course_id: str) -> None:
    """Best-effort rollback of a workspace created by the current operation."""
    path_service.get_course_workspace(course_id)
    anchor = _prepare_trusted_anchor(path_service)
    parents = ("user", "workspace", "course-mode", "courses")
    if not _supports_secure_dir_fd():
        current = anchor
        for component in parents:
            current = current / component
            _validate_directory(current)
        (current / course_id).rmdir()
        return

    parent_fd = _open_existing_directory_chain(anchor, parents)
    try:
        os.rmdir(course_id, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _open_existing_directory_chain(anchor: Path, components: tuple[str, ...]) -> int:
    current_fd = os.open(anchor, _directory_flags())
    try:
        for component in components:
            child_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
    except OSError as exc:
        os.close(current_fd)
        raise InvalidArtifactPathError(
            "Course storage contains a symlink or reparse point"
        ) from exc
    return current_fd


@contextmanager
def open_course_artifact_for_read(
    path_service: PathService,
    course_id: str,
    relative_path: str,
) -> Iterator[BinaryIO]:
    """Open an artifact for reading without exporting a prechecked path."""
    normalized = normalize_artifact_relative_path(relative_path)
    path_service.get_course_workspace(course_id)
    anchor = _prepare_trusted_anchor(path_service)
    components = (
        "user",
        "workspace",
        "course-mode",
        "courses",
        course_id,
        *normalized.split("/")[:-1],
    )
    leaf = normalized.split("/")[-1]

    if _supports_secure_dir_fd():
        parent_fd = _open_existing_directory_chain(anchor, components)
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            file_fd = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise InvalidArtifactPathError(
                "Artifact path contains a symlink or reparse point"
            ) from exc
        finally:
            os.close(parent_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(file_fd)
            raise InvalidArtifactPathError("Artifact is not a regular file")
        with os.fdopen(file_fd, "rb") as artifact:
            yield artifact
        return

    current = anchor
    for component in components:
        current = current / component
        _validate_directory(current)
    candidate = current / leaf
    file_fd: int | None = None
    try:
        before = candidate.lstat()
        if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise InvalidArtifactPathError("Artifact path contains a symlink or reparse point")
        file_fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        opened = os.fstat(file_fd)
        after = candidate.lstat()
    except InvalidArtifactPathError:
        if file_fd is not None:
            os.close(file_fd)
        raise
    except OSError as exc:
        if file_fd is not None:
            os.close(file_fd)
        raise InvalidArtifactPathError("Artifact file is unavailable") from exc
    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or (
        after.st_dev,
        after.st_ino,
    ) != (opened.st_dev, opened.st_ino):
        os.close(file_fd)
        raise InvalidArtifactPathError("Artifact changed while it was being opened")
    with os.fdopen(file_fd, "rb") as artifact:
        yield artifact


__all__ = [
    "InvalidArtifactPathError",
    "ensure_course_data_root",
    "ensure_course_workspace",
    "normalize_artifact_relative_path",
    "open_course_artifact_for_read",
    "remove_empty_course_workspace",
]
