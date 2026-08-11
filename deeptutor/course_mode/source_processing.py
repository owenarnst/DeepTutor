"""Course-owned source validation and the private ingestion seam.

Course Mode deliberately reuses DeepTutor's existing file router and bytes
extractor.  It does not register a Course as a user knowledge base: the
adapter receives Course-owned source paths and may build private retrieval
state under that Course's server-only workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Awaitable, Callable, Iterable, Protocol
import unicodedata
from urllib.parse import unquote, urlsplit

from deeptutor.services.rag.file_routing import FileTypeRouter
from deeptutor.utils.document_extractor import extract_text_from_bytes
from deeptutor.utils.document_validator import DocumentValidator

from .models import ManifestRole, ManifestVisibility


class InvalidCourseSourceError(ValueError):
    """Raised when learner-supplied Course source metadata/content is unsafe."""


@dataclass(frozen=True)
class RoleProposal:
    role: "ManifestRole"
    visibility: "ManifestVisibility"
    suspected_solution: bool
    role_confirmed: bool
    visibility_confirmed: bool


COURSE_SUPPORTED_EXTENSIONS = frozenset(
    FileTypeRouter.PARSER_EXTENSIONS | FileTypeRouter.TEXT_EXTENSIONS
)
COURSE_REJECTED_IMAGE_EXTENSIONS = frozenset(FileTypeRouter.IMAGE_EXTENSIONS | {".svg"})
COURSE_MAX_FILE_BYTES = DocumentValidator.MAX_FILE_SIZE
COURSE_MAX_TOTAL_BYTES = COURSE_MAX_FILE_BYTES * 5
COURSE_MAX_UPLOAD_COUNT = 32
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_BAD_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]")
_KNOWN_BINARY_MAGICS = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
    b"\x00\x00\x01\x00",
)


def _looks_like_binary_text(content: bytes) -> bool:
    """Reject control-heavy text-extension uploads before permissive decoding."""
    if any(byte < 0x20 and byte not in {0x09, 0x0A, 0x0C, 0x0D} for byte in content):
        return True
    if 0x7F in content:
        return True
    if len(content) < 32:
        return False
    printable = sum(
        byte in {0x09, 0x0A, 0x0C, 0x0D} or 0x20 <= byte <= 0x7E or byte >= 0xA0 for byte in content
    )
    return printable / len(content) < 0.85


def normalize_ocw_url(value: str) -> str:
    """Validate the learner's OCW URL without resolving or fetching it."""
    if not isinstance(value, str):
        raise InvalidCourseSourceError("OCW URL is required")
    supplied = value.strip()
    if not supplied:
        raise InvalidCourseSourceError("OCW URL is required")
    try:
        parsed = urlsplit(supplied)
        # Accessing .port catches malformed numeric ports without ever opening
        # a network connection.
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidCourseSourceError("OCW URL is invalid") from exc
    path_segments = parsed.path.split("/")
    # A single trailing slash is harmless and common in OCW links. Empty
    # interior segments (including a doubled trailing slash), however, make
    # the path ambiguous and are not part of the strict Course contract.
    if path_segments and path_segments[-1] == "":
        path_segments.pop()
    if (
        parsed.scheme != "https"
        or hostname != "ocw.mit.edu"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/courses/")
        or len(path_segments) < 3
        or path_segments[0] != ""
        or path_segments[1] != "courses"
        or any(segment in {"", ".", ".."} for segment in path_segments[2:])
    ):
        raise InvalidCourseSourceError(
            "OCW URL must be a credential-free HTTPS ocw.mit.edu course URL"
        )
    decoded_segments = tuple(unquote(segment) for segment in path_segments[2:])
    if any(
        segment in {"", ".", ".."}
        or "/" in segment
        or "\\" in segment
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in segment)
        for segment in decoded_segments
    ):
        raise InvalidCourseSourceError(
            "OCW URL must be a credential-free HTTPS ocw.mit.edu course URL"
        )
    return supplied


def sanitize_upload_filename(filename: str) -> str:
    """Return one safe display/storage basename, rejecting path input first."""
    if not isinstance(filename, str) or not filename.strip():
        raise InvalidCourseSourceError("Uploaded filename is required")
    normalized = unicodedata.normalize("NFC", filename.strip())
    if (
        "\x00" in normalized
        or "/" in normalized
        or "\\" in normalized
        or normalized in {".", ".."}
        or normalized.startswith("/")
        or _WINDOWS_DRIVE_PREFIX.match(normalized)
    ):
        raise InvalidCourseSourceError("Uploaded filename must be a relative basename")
    cleaned = _BAD_FILENAME_CHARS.sub("_", normalized).strip().strip(".")
    if not cleaned or cleaned in {".", ".."}:
        raise InvalidCourseSourceError("Uploaded filename is invalid")
    extension = PurePosixPath(cleaned).suffix.lower()
    if (
        extension in COURSE_REJECTED_IMAGE_EXTENSIONS
        or extension not in COURSE_SUPPORTED_EXTENSIONS
    ):
        raise InvalidCourseSourceError(
            f"Unsupported Course source format: {extension or 'unknown'}"
        )
    return cleaned


def prepare_upload_identity(
    filename: str,
    content: bytes,
    *,
    max_file_bytes: int = COURSE_MAX_FILE_BYTES,
) -> tuple[str, str, str, int]:
    """Validate and extract one upload, returning storage name/hash/text/size."""
    if not isinstance(content, bytes):
        raise InvalidCourseSourceError("Uploaded content is invalid")
    display_name = sanitize_upload_filename(filename)
    if not content:
        raise InvalidCourseSourceError(f"{display_name} is empty")
    if len(content) > max_file_bytes:
        raise InvalidCourseSourceError(f"{display_name} exceeds the upload size limit")
    extension = PurePosixPath(display_name).suffix.lower()
    if extension in FileTypeRouter.TEXT_EXTENSIONS:
        if b"\x00" in content or any(content.startswith(magic) for magic in _KNOWN_BINARY_MAGICS):
            raise InvalidCourseSourceError(f"{display_name} is not a text source")
        if _looks_like_binary_text(content):
            raise InvalidCourseSourceError(f"{display_name} is not a text source")
    if extension == ".pdf" and b"/Encrypt" in content:
        # The shared PDF extractor permits passwordless encrypted documents;
        # Course Mode keeps the source policy stricter and rejects encryption
        # rather than relying on an extractor-specific interpretation.
        raise InvalidCourseSourceError(f"{display_name} is encrypted")
    try:
        text = extract_text_from_bytes(
            display_name,
            content,
            max_bytes=max_file_bytes,
            max_chars=500_000,
        )
    except Exception as exc:
        # Never leak parser/provider details from the Course API. The caller
        # maps this stable domain failure to ``invalid_source_content``.
        raise InvalidCourseSourceError(f"{display_name} has no readable text") from exc
    meaningful_text = re.sub(r"---\s*Page\s+\d+\s*---", "", text).strip()
    if not meaningful_text:
        raise InvalidCourseSourceError(f"{display_name} has no readable text")
    return display_name, hashlib.sha256(content).hexdigest(), text, len(content)


def validate_upload_batch(
    uploads: Iterable[tuple[str, bytes]],
    *,
    max_file_bytes: int = COURSE_MAX_FILE_BYTES,
    max_total_bytes: int = COURSE_MAX_TOTAL_BYTES,
) -> tuple[tuple[str, str, str, bytes, str, int], ...]:
    """Validate/extract every file before any Course filesystem mutation."""
    prepared: list[tuple[str, str, str, bytes, str, int]] = []
    seen_names: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    total = 0
    for index, (filename, content) in enumerate(uploads):
        if index >= COURSE_MAX_UPLOAD_COUNT:
            raise InvalidCourseSourceError("Course source file count exceeds the request limit")
        display_name, content_hash, text, size = prepare_upload_identity(
            filename,
            content,
            max_file_bytes=max_file_bytes,
        )
        name_key = display_name.casefold()
        if name_key in seen_names:
            raise InvalidCourseSourceError(f"Duplicate filename after sanitization: {display_name}")
        identity = (display_name, content_hash)
        if identity in seen_identities:
            raise InvalidCourseSourceError("Duplicate uploaded source identity")
        seen_names.add(name_key)
        seen_identities.add(identity)
        total += size
        if total > max_total_bytes:
            raise InvalidCourseSourceError("Uploaded Course sources exceed the batch size limit")
        prepared.append((filename, display_name, content_hash, content, text, size))
    if not prepared:
        raise InvalidCourseSourceError("At least one supported text-extractable source is required")
    return tuple(prepared)


def infer_manifest_role(filename: str) -> RoleProposal:
    """Make a conservative filename-only proposal; learner review is explicit."""
    name = PurePosixPath(filename).stem.lower().replace("-", "_").replace(" ", "_")
    tokens = {token for token in re.split(r"[_+.]+", name) if token}
    suspected_solution = bool(
        tokens & {"solution", "solutions", "answer", "answers", "key", "keys", "soln"}
    )
    if suspected_solution:
        return RoleProposal(
            role=ManifestRole.SOLUTION,
            visibility=ManifestVisibility.INSTRUCTOR_ONLY,
            suspected_solution=True,
            role_confirmed=False,
            visibility_confirmed=False,
        )
    if tokens & {"syllabus", "schedule", "overview"}:
        role = ManifestRole.SYLLABUS
    elif tokens & {"lecture", "lectures", "note", "notes", "slides", "slide"}:
        role = ManifestRole.LECTURE_NOTE
    elif tokens & {"reading", "readings", "article", "paper", "textbook"}:
        role = ManifestRole.READING
    elif tokens & {"assignment", "assignments", "homework", "problem", "problems", "pset"}:
        role = ManifestRole.ASSIGNMENT
    elif tokens & {"grading", "rubric", "rubrics", "grade"}:
        role = ManifestRole.GRADING_RESOURCE
    else:
        role = ManifestRole.UNKNOWN
    return RoleProposal(
        role=role,
        visibility=ManifestVisibility.LEARNER_VISIBLE,
        suspected_solution=False,
        role_confirmed=role is not ManifestRole.UNKNOWN,
        visibility_confirmed=True,
    )


class CourseIngestionAdapter(Protocol):
    """Private seam for the existing extraction/indexing machinery."""

    def index_sources(
        self,
        *,
        course_id: str,
        source_paths: tuple[Path, ...],
        workspace: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None | Awaitable[None]: ...


class DefaultCourseIngestionAdapter:
    """Safe local adapter that records a Course-private text index manifest.

    Parsing is delegated to ``document_extractor`` before this adapter runs.
    The index metadata is intentionally under server-private Course storage and
    never enters the user KB registry. Deployments can inject the existing RAG
    provider behind this interface without changing Course ownership.
    """

    def __init__(self, path_service: object | None = None) -> None:
        self.path_service = path_service

    def index_sources(
        self,
        *,
        course_id: str,
        source_paths: tuple[Path, ...],
        workspace: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        _ = workspace
        payload = {
            "sources": [path.name for path in source_paths],
            "count": len(source_paths),
        }
        if self.path_service is not None:
            # Import locally to keep the source-processing module's parser
            # seam independent from filesystem implementation details.
            from .artifacts import write_course_artifact_atomic

            write_course_artifact_atomic(
                self.path_service,  # type: ignore[arg-type]
                course_id,
                "private-index/sources.json",
                json.dumps(payload, sort_keys=True).encode("utf-8"),
            )
        if progress:
            progress(len(source_paths), len(source_paths))


__all__ = [
    "COURSE_MAX_FILE_BYTES",
    "COURSE_MAX_TOTAL_BYTES",
    "COURSE_MAX_UPLOAD_COUNT",
    "COURSE_REJECTED_IMAGE_EXTENSIONS",
    "COURSE_SUPPORTED_EXTENSIONS",
    "CourseIngestionAdapter",
    "DefaultCourseIngestionAdapter",
    "InvalidCourseSourceError",
    "RoleProposal",
    "infer_manifest_role",
    "normalize_ocw_url",
    "prepare_upload_identity",
    "sanitize_upload_filename",
    "validate_upload_batch",
]
