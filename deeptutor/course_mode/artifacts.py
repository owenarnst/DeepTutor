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


class CourseStorageMigrationError(RuntimeError):
    """Raised when legacy Course storage cannot be moved without ambiguity."""


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


def _try_open_existing_directory_chain(
    anchor: Path,
    components: tuple[str, ...],
) -> int | None:
    """Open a no-follow directory chain, returning ``None`` when it is absent."""
    current_fd = os.open(anchor, _directory_flags())
    try:
        for component in components:
            try:
                child_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                os.close(current_fd)
                return None
            os.close(current_fd)
            current_fd = child_fd
    except OSError as exc:
        os.close(current_fd)
        raise InvalidArtifactPathError(
            "Legacy Course storage contains a symlink or reparse point"
        ) from exc
    return current_fd


def _open_migration_entry(parent_fd: int, name: str, *, directory: bool) -> int | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        entry_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CourseStorageMigrationError(
            "Legacy Course storage entry is not safe to migrate"
        ) from exc
    metadata = os.fstat(entry_fd)
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected:
        os.close(entry_fd)
        raise CourseStorageMigrationError("Legacy Course storage entry has an invalid type")
    return entry_fd


def _move_legacy_entry(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    *,
    directory: bool,
) -> None:
    """Atomically move one legacy entry and prove the held inode was moved."""
    source_fd = _open_migration_entry(source_parent_fd, source_name, directory=directory)
    if source_fd is None:
        return
    try:
        destination_fd = _open_migration_entry(
            destination_parent_fd,
            destination_name,
            directory=directory,
        )
        if destination_fd is not None:
            os.close(destination_fd)
            raise CourseStorageMigrationError(
                "Legacy and private Course storage both exist; refusing to overwrite"
            )
        source_identity = os.fstat(source_fd)
        try:
            os.rename(
                source_name,
                destination_name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=destination_parent_fd,
            )
        except FileNotFoundError:
            # Another trusted request may have completed the same atomic move.
            _verify_moved_migration_entry(
                source_identity,
                destination_parent_fd,
                destination_name,
                source_parent_fd,
                source_name,
                directory=directory,
                missing_message="Legacy Course storage changed during migration",
                mismatch_message="Legacy Course storage changed during concurrent migration",
            )
            return
        except OSError as exc:
            raise CourseStorageMigrationError("Could not move legacy Course storage") from exc

        _verify_moved_migration_entry(
            source_identity,
            destination_parent_fd,
            destination_name,
            source_parent_fd,
            source_name,
            directory=directory,
            missing_message="Legacy Course storage disappeared during migration",
            mismatch_message="Legacy Course storage was replaced during migration",
        )
    finally:
        os.close(source_fd)


def _verify_moved_migration_entry(
    source_identity: os.stat_result,
    destination_parent_fd: int,
    destination_name: str,
    legacy_parent_fd: int,
    legacy_name: str,
    *,
    directory: bool,
    missing_message: str,
    mismatch_message: str,
) -> None:
    """Verify an atomic migration moved the exact entry held by the caller."""
    moved_fd = _open_migration_entry(
        destination_parent_fd,
        destination_name,
        directory=directory,
    )
    if moved_fd is None:
        raise CourseStorageMigrationError(missing_message)
    try:
        moved_identity = os.fstat(moved_fd)
        if (source_identity.st_dev, source_identity.st_ino) != (
            moved_identity.st_dev,
            moved_identity.st_ino,
        ):
            _quarantine_migration_destination(
                destination_parent_fd,
                destination_name,
                legacy_parent_fd,
                legacy_name,
            )
            raise CourseStorageMigrationError(mismatch_message)
    finally:
        os.close(moved_fd)


def _quarantine_migration_destination(
    destination_parent_fd: int,
    destination_name: str,
    legacy_parent_fd: int,
    legacy_name: str,
) -> None:
    """Move an unverified raced entry back out of server-private storage."""
    quarantine_name = f".{legacy_name}.migration-rejected-{uuid4()}"
    try:
        os.rename(
            destination_name,
            quarantine_name,
            src_dir_fd=destination_parent_fd,
            dst_dir_fd=legacy_parent_fd,
        )
    except OSError as exc:
        raise CourseStorageMigrationError(
            "Could not quarantine replaced legacy Course storage"
        ) from exc


def migrate_legacy_course_storage(path_service: PathService) -> None:
    """Move pre-private Course DB/workspace paths into tenant-private storage.

    Migration is atomic per top-level entry, never overwrites a destination,
    and holds source/destination directory handles throughout each rename. A
    repeated call is a no-op because the legacy names no longer exist.
    """
    anchor = _prepare_trusted_anchor(path_service)
    ensure_course_data_root(path_service)
    try:
        scope_components = path_service.workspace_root.relative_to(anchor).parts
    except ValueError as exc:
        raise CourseStorageMigrationError(
            "Legacy Course storage is outside the trusted data anchor"
        ) from exc

    destination_fd = _open_existing_directory_chain(
        anchor,
        _course_storage_components(path_service),
    )
    try:
        legacy_user_fd = _try_open_existing_directory_chain(
            anchor,
            (*scope_components, "user"),
        )
        if legacy_user_fd is not None:
            try:
                _move_legacy_entry(
                    legacy_user_fd,
                    "course_mode.db",
                    destination_fd,
                    "course_mode.db",
                    directory=False,
                )
            finally:
                os.close(legacy_user_fd)

        legacy_workspace_fd = _try_open_existing_directory_chain(
            anchor,
            (*scope_components, "user", "workspace"),
        )
        if legacy_workspace_fd is not None:
            try:
                _move_legacy_entry(
                    legacy_workspace_fd,
                    "course-mode",
                    destination_fd,
                    "workspace",
                    directory=True,
                )
            finally:
                os.close(legacy_workspace_fd)
    finally:
        os.close(destination_fd)


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
    "CourseStorageMigrationError",
    "InvalidArtifactPathError",
    "UnsupportedCourseStorageError",
    "ensure_course_data_root",
    "ensure_course_workspace",
    "migrate_legacy_course_storage",
    "normalize_artifact_relative_path",
    "open_course_artifact_for_read",
    "remove_empty_course_workspace",
]
