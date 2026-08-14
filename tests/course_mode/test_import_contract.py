from __future__ import annotations

import pytest

from deeptutor.course_mode.models import ManifestRole, ManifestVisibility
from deeptutor.course_mode.source_processing import (
    COURSE_MAX_FILE_BYTES,
    COURSE_MAX_PDF_BYTES,
    COURSE_MAX_TOTAL_BYTES,
    COURSE_MAX_UPLOAD_COUNT,
    InvalidCourseSourceError,
    infer_manifest_role,
    normalize_ocw_url,
    prepare_upload_identity,
    sanitize_upload_filename,
    validate_upload_batch,
)
from deeptutor.utils.document_extractor import (
    MAX_OOXML_EXPANDED_BYTES,
    MAX_OOXML_MEMBER_COUNT,
    MAX_OOXML_MEMBER_EXPANDED_BYTES,
)


def _ooxml_package(
    *,
    member_count: int = 1,
    extra_payload: bytes = b"x",
    payload: bytes = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>safe</w:t></w:r></w:p></w:body></w:document>"
    ),
) -> bytes:
    import io
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", payload)
        for index in range(member_count - 1):
            archive.writestr(f"word/extra-{index}.xml", extra_payload)
    return stream.getvalue()


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


def test_upload_request_has_a_file_count_budget() -> None:
    uploads = [(f"note-{index}.md", b"text") for index in range(COURSE_MAX_UPLOAD_COUNT + 1)]

    with pytest.raises(InvalidCourseSourceError, match="file count"):
        validate_upload_batch(uploads)


def test_course_upload_budgets_fit_document_and_proxy_policy() -> None:
    assert COURSE_MAX_FILE_BYTES == 100 * 1024 * 1024
    assert COURSE_MAX_PDF_BYTES == 50 * 1024 * 1024
    assert COURSE_MAX_TOTAL_BYTES == 200 * 1024 * 1024
    assert COURSE_MAX_TOTAL_BYTES < 210 * 1024 * 1024


def test_normal_docx_is_extractable_after_ooxml_safety_check() -> None:
    display, content_hash, text, size = prepare_upload_identity("lecture.docx", _ooxml_package())

    assert display == "lecture.docx"
    assert content_hash
    assert "safe" in text
    assert size > 0


def test_ooxml_member_count_bomb_is_rejected_before_parser() -> None:
    payload = _ooxml_package(member_count=MAX_OOXML_MEMBER_COUNT + 1)

    with pytest.raises(InvalidCourseSourceError, match="no readable text"):
        prepare_upload_identity("lecture.docx", payload)


def test_ooxml_expanded_member_bomb_is_rejected_before_parser() -> None:
    payload = _ooxml_package(
        payload=b"<w:p>" + (b"x" * (MAX_OOXML_MEMBER_EXPANDED_BYTES + 1)) + b"</w:p>"
    )

    with pytest.raises(InvalidCourseSourceError, match="no readable text"):
        prepare_upload_identity("lecture.docx", payload)


def test_ooxml_aggregate_expansion_bomb_is_rejected_before_parser() -> None:
    import random

    member_size = (MAX_OOXML_EXPANDED_BYTES // 8) - 32
    random_source = random.Random(7)
    payload = random_source.randbytes(member_size)
    package = _ooxml_package(member_count=9, payload=payload, extra_payload=payload)

    with pytest.raises(InvalidCourseSourceError, match="no readable text"):
        prepare_upload_identity("lecture.docx", package)


def test_ooxml_compression_ratio_bomb_is_rejected_before_parser() -> None:
    import io
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:p>" + (b"A" * 1_000_000) + b"</w:p>")

    with pytest.raises(InvalidCourseSourceError, match="no readable text"):
        prepare_upload_identity("lecture.docx", stream.getvalue())
