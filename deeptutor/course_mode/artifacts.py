"""Portable artifact identities and contained Course workspace resolution.

Artifact paths are stored as canonical POSIX-relative strings. Filesystem
sinks must call :func:`resolve_course_artifact_path`; it rejects every
existing symlink component instead of following it, even when that symlink
would currently resolve back inside the workspace. This keeps immutable
artifact identity independent from mutable link targets.
"""

from __future__ import annotations

from pathlib import Path
import re

from deeptutor.services.path_service import PathService

_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


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


def resolve_course_artifact_path(
    path_service: PathService,
    course_id: str,
    relative_path: str,
) -> Path:
    """Resolve an artifact path while enforcing containment and no symlinks."""
    normalized = normalize_artifact_relative_path(relative_path)
    workspace = path_service.get_course_workspace(course_id)
    course_mode_root = path_service.get_course_mode_workspace_root().resolve()

    if workspace.is_symlink():
        raise InvalidArtifactPathError("Course workspace must not be a symlink")
    resolved_workspace = workspace.resolve()
    try:
        resolved_workspace.relative_to(course_mode_root)
    except ValueError as exc:
        raise InvalidArtifactPathError("Course workspace escapes Course Mode root") from exc

    candidate = workspace
    for component in normalized.split("/"):
        candidate = candidate / component
        if candidate.is_symlink():
            raise InvalidArtifactPathError("Artifact path contains a symlink component")

    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_workspace)
    except ValueError as exc:
        raise InvalidArtifactPathError("Artifact path escapes course workspace") from exc
    return candidate


__all__ = [
    "InvalidArtifactPathError",
    "normalize_artifact_relative_path",
    "resolve_course_artifact_path",
]
