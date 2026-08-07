"""Domain models owned by Course Mode.

These intentionally do not inherit Mastery Path's progress aggregate. A Course
owns independently identified Units and stable references to immutable
artifacts; later children can add policies without changing this storage seam.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CourseStatus(StrEnum):
    DRAFT = "draft"


class Unit(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    title: str
    position: int


class Course(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str
    status: CourseStatus
    workspace_ref: str
    created_at: str
    updated_at: str
    units: tuple[Unit, ...]


class ArtifactReference(BaseModel):
    """Stable SQL identity for a large immutable artifact stored on disk."""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    unit_id: str | None = None
    kind: str
    relative_path: str
    content_hash: str
    created_at: str
