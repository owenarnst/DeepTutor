from __future__ import annotations

import pytest

from deeptutor.course_mode.models import ManifestRole, ManifestVisibility
from deeptutor.course_mode.source_processing import (
    InvalidCourseSourceError,
    infer_manifest_role,
    normalize_ocw_url,
    prepare_upload_identity,
    sanitize_upload_filename,
    validate_upload_batch,
)


def test_ocw_url_is_strict_and_keeps_the_original_path() -> None:
    supplied = "  https://ocw.mit.edu/courses/18-06sc-linear-algebra-fall-2011/  "

    assert normalize_ocw_url(supplied) == supplied.strip()


@pytest.mark.parametrize(
    "url",
    [
        "http://ocw.mit.edu/courses/18-06/",
        "https://user:pass@ocw.mit.edu/courses/18-06/",
        "https://example.com/courses/18-06/",
        "https://ocw.mit.edu:8443/courses/18-06/",
        "https://ocw.mit.edu/",
        "https://ocw.mit.edu/courses/",
        "https://ocw.mit.edu/courses//18-06/",
        "https://ocw.mit.edu/courses/18-06//",
        "https://ocw.mit.edu/courses/%2e%2e/private/",
    ],
)
def test_ocw_url_rejects_fetch_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(InvalidCourseSourceError):
        normalize_ocw_url(url)


@pytest.mark.parametrize(
    "filename",
    ["../notes.md", "/tmp/notes.md", r"C:\notes.md", "notes\\lesson.md", ".."],
)
def test_upload_filename_rejects_path_components_before_sanitizing(filename: str) -> None:
    with pytest.raises(InvalidCourseSourceError):
        sanitize_upload_filename(filename)


def test_manifest_role_inference_marks_solution_as_instructor_only_review() -> None:
    proposal = infer_manifest_role("week-2-solution.pdf")

    assert proposal.role is ManifestRole.SOLUTION
    assert proposal.visibility is ManifestVisibility.INSTRUCTOR_ONLY
    assert proposal.suspected_solution is True
    assert proposal.role_confirmed is False
    assert proposal.visibility_confirmed is False


def test_unknown_manifest_role_is_a_blocking_unconfirmed_proposal() -> None:
    proposal = infer_manifest_role("week-2-material.pdf")

    assert proposal.role is ManifestRole.UNKNOWN
    assert proposal.role_confirmed is False


@pytest.mark.parametrize("filename", ["diagram.svg", "photo.png", "recording.mp3", "bundle.zip"])
def test_non_text_sources_are_rejected_even_when_the_name_is_known(filename: str) -> None:
    with pytest.raises(InvalidCourseSourceError):
        prepare_upload_identity(filename, b"not an accepted Course source")


def test_control_heavy_text_extension_is_rejected_as_binary() -> None:
    with pytest.raises(InvalidCourseSourceError, match="not a text source"):
        prepare_upload_identity("notes.txt", bytes(range(1, 32)) * 4)


def test_upload_batch_rejects_case_colliding_sanitized_names() -> None:
    with pytest.raises(InvalidCourseSourceError, match="Duplicate filename"):
        validate_upload_batch([("Lecture.md", b"one"), ("lecture.md", b"two")])
