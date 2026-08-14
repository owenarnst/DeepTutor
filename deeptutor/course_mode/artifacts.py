"""Portable artifact identities and operation-owned Course workspace access.

Artifact paths are stored as canonical POSIX-relative strings. Filesystem
sinks use the functions in this module so validation and access happen in one
operation. Existing symlink or reparse-point components are rejected, even
when they currently resolve back inside the workspace. This keeps immutable
artifact identity independent from mutable link targets.

Platforms without directory-handle-relative no-follow operations are not
supported for Course storage and fail closed before touching the filesystem.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
from typing import BinaryIO, Iterator
from uuid import uuid4

from deeptutor.services.path_service import PathService

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_SECURE_DIR_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)


class InvalidArtifactPathError(ValueError):
    pass


class UnsupportedCourseStorageError(RuntimeError):
    """Raised when the platform cannot provide race-safe Course filesystem access."""


def _require_handle_relative_storage() -> None:
    if not _supports_secure_dir_fd():
        raise UnsupportedCourseStorageError(
            "Course storage requires handle-relative no-follow filesystem operations"
        )


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
    """Create and validate the server-only anchor outside runner mounts.

    The anchor itself is deployment-owned (normally ``data/``); all Course
    descendants are opened handle-relatively. Platforms without that API fail
    closed before touching the filesystem.
    """
    _require_handle_relative_storage()
    anchor = path_service.get_course_storage_anchor()
    anchor.mkdir(parents=True, exist_ok=True)
    _validate_directory(anchor)
    return anchor


def _course_storage_components(path_service: PathService) -> tuple[str, ...]:
    try:
        components = (
            path_service.get_course_storage_root()
            .relative_to(path_service.get_course_storage_anchor())
            .parts
        )
    except ValueError as exc:
        raise InvalidArtifactPathError("Unexpected Course storage layout") from exc
    if not components or any(component in {"", ".", ".."} for component in components):
        raise InvalidArtifactPathError("Unexpected Course storage layout")
    return components


def _ensure_directory_chain(path_service: PathService, components: tuple[str, ...]) -> bool:
    anchor = _prepare_trusted_anchor(path_service)
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
    """Ensure the tenant's server-private Course root without following links."""
    _ensure_directory_chain(path_service, _course_storage_components(path_service))


def ensure_course_workspace(path_service: PathService, course_id: str) -> bool:
    """Atomically ensure one Course workspace; return whether its leaf was created."""
    path_service.get_course_workspace(course_id)  # Canonical UUID validation.
    return _ensure_directory_chain(
        path_service,
        (*_course_storage_components(path_service), "workspace", "courses", course_id),
    )


def ensure_course_private_directory(
    path_service: PathService,
    course_id: str,
    relative_path: str,
) -> Path:
    """Create one Course-owned directory chain with no-follow traversal."""
    normalized = normalize_artifact_relative_path(relative_path)
    path_service.get_course_workspace(course_id)
    components = (
        *_course_storage_components(path_service),
        "workspace",
        "courses",
        course_id,
        *normalized.split("/"),
    )
    _ensure_directory_chain(path_service, components)
    return path_service.get_course_workspace(course_id) / normalized


def write_course_artifact_atomic(
    path_service: PathService,
    course_id: str,
    relative_path: str,
    content: bytes,
) -> None:
    """Atomically write one Course artifact through no-follow descriptors.

    The operation never opens a caller-derived absolute path. Directory
    components are created and reopened with ``O_NOFOLLOW``; the temporary
    file and final rename are relative to the stable parent descriptor.
    """
    normalized = normalize_artifact_relative_path(relative_path)
    if not isinstance(content, bytes):
        raise InvalidArtifactPathError("Artifact content must be bytes")
    path_service.get_course_workspace(course_id)
    _require_handle_relative_storage()
    parent_components = (
        *_course_storage_components(path_service),
        "workspace",
        "courses",
        course_id,
        *normalized.split("/")[:-1],
    )
    _ensure_directory_chain(path_service, parent_components)
    anchor = _prepare_trusted_anchor(path_service)
    parent_fd = _open_existing_directory_chain(anchor, parent_components)
    leaf = normalized.split("/")[-1]
    temporary_name = f".{leaf}.{uuid4().hex}.tmp"
    temp_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        temp_fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(content)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError("Course artifact write made no progress")
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.rename(temporary_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError:
            # Some filesystems do not permit directory fsync; the rename is
            # still atomic and the descriptor boundary remains safe.
            pass
    except OSError as exc:
        raise InvalidArtifactPathError("Course artifact could not be written safely") from exc
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except OSError:
            pass
        os.close(parent_fd)


def remove_course_artifact(path_service: PathService, course_id: str, relative_path: str) -> None:
    """Best-effort no-follow removal used to compensate a failed batch."""
    normalized = normalize_artifact_relative_path(relative_path)
    path_service.get_course_workspace(course_id)
    anchor = _prepare_trusted_anchor(path_service)
    parent_components = (
        *_course_storage_components(path_service),
        "workspace",
        "courses",
        course_id,
        *normalized.split("/")[:-1],
    )
    parent_fd = _open_existing_directory_chain(anchor, parent_components)
    try:
        os.unlink(normalized.split("/")[-1], dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(parent_fd)


def remove_empty_course_directory(
    path_service: PathService,
    course_id: str,
    relative_path: str,
) -> None:
    """Remove one operation-created empty directory without following links."""
    normalized = normalize_artifact_relative_path(relative_path)
    path_service.get_course_workspace(course_id)
    anchor = _prepare_trusted_anchor(path_service)
    parent_components = (
        *_course_storage_components(path_service),
        "workspace",
        "courses",
        course_id,
        *normalized.split("/")[:-1],
    )
    parent_fd = _open_existing_directory_chain(anchor, parent_components)
    try:
        try:
            os.rmdir(normalized.split("/")[-1], dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(parent_fd)


def remove_empty_course_workspace(path_service: PathService, course_id: str) -> None:
    """Best-effort rollback of a workspace created by the current operation."""
    path_service.get_course_workspace(course_id)
    anchor = _prepare_trusted_anchor(path_service)
    parents = (*_course_storage_components(path_service), "workspace", "courses")

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
        *_course_storage_components(path_service),
        "workspace",
        "courses",
        course_id,
        *normalized.split("/")[:-1],
    )
    leaf = normalized.split("/")[-1]

    parent_fd = _open_existing_directory_chain(anchor, components)
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(leaf, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise InvalidArtifactPathError("Artifact path contains a symlink or reparse point") from exc
    finally:
        os.close(parent_fd)
    metadata = os.fstat(file_fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(file_fd)
        raise InvalidArtifactPathError("Artifact is not a regular file")
    with os.fdopen(file_fd, "rb") as artifact:
        yield artifact


__all__ = [
    "InvalidArtifactPathError",
    "UnsupportedCourseStorageError",
    "ensure_course_data_root",
    "ensure_course_private_directory",
    "ensure_course_workspace",
    "normalize_artifact_relative_path",
    "open_course_artifact_for_read",
    "remove_course_artifact",
    "remove_empty_course_directory",
    "remove_empty_course_workspace",
    "write_course_artifact_atomic",
]
