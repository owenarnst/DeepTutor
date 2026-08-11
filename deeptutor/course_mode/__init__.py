"""Course Mode bounded context."""

from .artifacts import (
    InvalidArtifactPathError,
    UnsupportedCourseStorageError,
    ensure_course_data_root,
    ensure_course_workspace,
    normalize_artifact_relative_path,
    open_course_artifact_for_read,
)
from .models import ArtifactReference, Course, CourseStatus, Unit
from .repository import CourseInput, CoursePage, CourseRepository

__all__ = [
    "ArtifactReference",
    "Course",
    "CourseInput",
    "CoursePage",
    "CourseRepository",
    "CourseStatus",
    "InvalidArtifactPathError",
    "UnsupportedCourseStorageError",
    "Unit",
    "ensure_course_data_root",
    "ensure_course_workspace",
    "normalize_artifact_relative_path",
    "open_course_artifact_for_read",
]
