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


class CourseJobStatus(StrEnum):
    """Public, restart-safe state of a Course source-processing job."""

    QUEUED = "queued"
    SOURCE_PROCESSING = "source_processing"
    AWAITING_MANIFEST_REVIEW = "awaiting_manifest_review"
    COMPLETED = "completed"
    FAILED = "failed"


class CourseJobStage(StrEnum):
    """Durable stages at which a Course job can pause or fail."""

    QUEUED = "queued"
    SOURCE_PROCESSING = "source_processing"
    MANIFEST_REVIEW = "awaiting_manifest_review"
    COMPLETED = "completed"


class ManifestRole(StrEnum):
    SYLLABUS = "syllabus"
    LECTURE_NOTE = "lecture_note"
    READING = "reading"
    ASSIGNMENT = "assignment"
    SOLUTION = "solution"
    GRADING_RESOURCE = "grading_resource"
    UNKNOWN = "unknown"


class ManifestVisibility(StrEnum):
    LEARNER_VISIBLE = "learner_visible"
    INSTRUCTOR_ONLY = "instructor_only"


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
    desired_outcome: str = ""
    weekly_minutes: int = 0
    ocw_url: str = ""
    scheduling: str | None = None
    difficulty: str | None = None
    accessibility: str | None = None
    processing_job_id: str | None = None


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


class CourseSource(BaseModel):
    """Course-owned identity for one accepted uploaded source."""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    unit_id: str | None = None
    original_filename: str
    display_filename: str
    relative_path: str
    content_hash: str
    size_bytes: int
    extracted_chars: int = 0
    created_at: str


class ManifestEntry(BaseModel):
    """One stable reviewable entry derived from one accepted source."""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    source_id: str
    original_filename: str
    display_filename: str
    role: ManifestRole
    visibility: ManifestVisibility
    suspected_solution: bool = False
    role_confirmed: bool = True
    visibility_confirmed: bool = True
    created_at: str
    updated_at: str

    @property
    def requires_review(self) -> bool:
        return self.role is ManifestRole.UNKNOWN or (
            self.suspected_solution and (not self.role_confirmed or not self.visibility_confirmed)
        )


class CourseProcessingJob(BaseModel):
    """Persisted identity and status for source processing."""

    model_config = ConfigDict(frozen=True)

    id: str
    course_id: str
    status: CourseJobStatus
    stage: CourseJobStage
    failed_stage: CourseJobStage | None = None
    error_code: str | None = None
    attempt_count: int = 0
    manifest_revision: int = 0
    created_at: str
    updated_at: str


class CourseManifest(BaseModel):
    """Manifest plus its optimistic-concurrency revision and blockers."""

    model_config = ConfigDict(frozen=True)

    course_id: str
    revision: int
    entries: tuple[ManifestEntry, ...]
    blockers: tuple[str, ...] = ()
    eligible_for_planning: bool = False
