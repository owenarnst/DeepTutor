"""Course-owned source validation and the private ingestion seam.

Course Mode deliberately reuses DeepTutor's existing file router and bytes
extractor.  It does not register a Course as a user knowledge base: the
adapter receives Course-owned source paths and may build private retrieval
state under that Course's server-only workspace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Awaitable, Callable, Iterable, Protocol
import unicodedata
from urllib.parse import unquote, urlsplit
from uuid import UUID

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


def validate_upload_batch_identity(
    uploads: Iterable[tuple[str, bytes]],
    *,
    max_file_bytes: int = COURSE_MAX_FILE_BYTES,
    max_total_bytes: int = COURSE_MAX_TOTAL_BYTES,
) -> tuple[tuple[str, str, str, bytes, int], ...]:
    """Validate cheap upload identity constraints without parsing file content.

    This pass is safe to use before an idempotency replay lookup: it still
    rejects path/extension/empty/size/count/duplicate violations, while
    avoiding expensive Office/PDF/text extraction for a known accepted retry.
    New requests always run the complete parser-backed validation afterwards.
    """
    identities: list[tuple[str, str, str, bytes, int]] = []
    seen_names: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()
    total = 0
    for index, (filename, content) in enumerate(uploads):
        if index >= COURSE_MAX_UPLOAD_COUNT:
            raise InvalidCourseSourceError("Course source file count exceeds the request limit")
        if not isinstance(content, bytes):
            raise InvalidCourseSourceError("Uploaded content is invalid")
        display_name = sanitize_upload_filename(filename)
        if not content:
            raise InvalidCourseSourceError(f"{display_name} is empty")
        size = len(content)
        if size > max_file_bytes:
            raise InvalidCourseSourceError(f"{display_name} exceeds the upload size limit")
        content_hash = hashlib.sha256(content).hexdigest()
        name_key = display_name.casefold()
        identity = (display_name, content_hash)
        if name_key in seen_names:
            raise InvalidCourseSourceError(f"Duplicate filename after sanitization: {display_name}")
        if identity in seen_identities:
            raise InvalidCourseSourceError("Duplicate uploaded source identity")
        seen_names.add(name_key)
        seen_identities.add(identity)
        total += size
        if total > max_total_bytes:
            raise InvalidCourseSourceError("Uploaded Course sources exceed the batch size limit")
        identities.append((filename, display_name, content_hash, content, size))
    if not identities:
        raise InvalidCourseSourceError("At least one supported text-extractable source is required")
    return tuple(identities)


def validate_upload_batch(
    uploads: Iterable[tuple[str, bytes]],
    *,
    max_file_bytes: int = COURSE_MAX_FILE_BYTES,
    max_total_bytes: int = COURSE_MAX_TOTAL_BYTES,
) -> tuple[tuple[str, str, str, bytes, str, int], ...]:
    """Validate/extract every file before any Course filesystem mutation."""
    prepared: list[tuple[str, str, str, bytes, str, int]] = []
    uploads = tuple(uploads)
    identities = validate_upload_batch_identity(
        uploads,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    for filename, display_name, content_hash, content, size in identities:
        display_name, content_hash, text, size = prepare_upload_identity(
            filename,
            content,
            max_file_bytes=max_file_bytes,
        )
        prepared.append((filename, display_name, content_hash, content, text, size))
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
    """Private seam for the existing extraction/indexing/retrieval machinery."""

    def index_sources(
        self,
        *,
        course_id: str,
        source_paths: tuple[Path, ...],
        workspace: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None | Awaitable[None]: ...

    def search(
        self,
        *,
        course_id: str,
        query: str,
        workspace: Path,
        top_k: int = 5,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


class DefaultCourseIngestionAdapter:
    """Index and retrieve through DeepTutor's RAG service in Course storage.

    The normal path invokes the repository's configured ``RAGService`` against
    a Course-private namespace. It never creates a user-KB config entry, and
    every source path is checked with no-follow metadata before the RAG engine
    sees it. A bounded lexical index is retained only for minimal installations
    where the optional LlamaIndex engine cannot import; it is still searchable,
    persisted under the Course workspace, and never a user-KB metadata stub.
    """

    def __init__(
        self,
        path_service: object | None = None,
        *,
        rag_service_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.path_service = path_service
        self.rag_service_factory = rag_service_factory

    @staticmethod
    def _is_link_or_reparse(metadata: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        )

    def _trusted_source_paths(
        self,
        source_paths: tuple[Path, ...],
        workspace: Path,
    ) -> tuple[Path, ...]:
        """Return only regular files reached through a stable Course directory."""
        workspace_path = Path(workspace)
        if not workspace_path.is_absolute():
            raise InvalidCourseSourceError("Course source workspace is invalid")
        workspace_metadata = workspace_path.lstat()
        if self._is_link_or_reparse(workspace_metadata) or not stat.S_ISDIR(
            workspace_metadata.st_mode
        ):
            raise InvalidCourseSourceError("Course source workspace is invalid")

        trusted: list[Path] = []
        for candidate in source_paths:
            candidate_path = Path(candidate)
            if not candidate_path.is_absolute():
                raise InvalidCourseSourceError("Course source path is invalid")
            try:
                relative = candidate_path.relative_to(workspace_path)
            except ValueError as exc:
                raise InvalidCourseSourceError(
                    "Course source path is outside its workspace"
                ) from exc
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise InvalidCourseSourceError("Course source path is invalid")
            current = workspace_path
            for index, component in enumerate(relative.parts):
                current = current / component
                metadata = current.lstat()
                if self._is_link_or_reparse(metadata):
                    raise InvalidCourseSourceError("Course source path contains a link")
                if index == len(relative.parts) - 1 and not stat.S_ISREG(metadata.st_mode):
                    raise InvalidCourseSourceError("Course source is not a regular file")
            trusted.append(current)
        if not trusted:
            raise InvalidCourseSourceError("Course source inventory is empty")
        return tuple(trusted)

    def _private_index_root(self, course_id: str, workspace: Path) -> Path:
        try:
            if str(UUID(course_id)) != course_id:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise InvalidCourseSourceError("Course identity is invalid") from exc
        if self.path_service is not None:
            from .artifacts import ensure_course_private_directory

            return ensure_course_private_directory(self.path_service, course_id, "private-index")
        root = Path(workspace) / "private-index"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _rag_service(self, private_root: Path) -> Any:
        factory = self.rag_service_factory
        if factory is not None:
            return factory(kb_base_dir=private_root, provider="llamaindex")
        from deeptutor.services.rag.service import RAGService

        return RAGService(kb_base_dir=str(private_root), provider="llamaindex")

    async def _maybe_await(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    def _write_fallback_index(
        self,
        course_id: str,
        private_root: Path,
        source_paths: tuple[Path, ...],
    ) -> None:
        records: list[dict[str, str]] = []
        for source_path in source_paths:
            with source_path.open("rb") as handle:
                content = handle.read(COURSE_MAX_FILE_BYTES + 1)
            if len(content) > COURSE_MAX_FILE_BYTES:
                raise InvalidCourseSourceError("Course source exceeds the size limit")
            text = extract_text_from_bytes(
                source_path.name,
                content,
                max_bytes=COURSE_MAX_FILE_BYTES,
                max_chars=500_000,
            ).strip()
            if text:
                records.append({"filename": source_path.name, "text": text})
        if not records:
            raise InvalidCourseSourceError("Course source inventory has no searchable text")
        payload = json.dumps({"records": records}, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        relative_path = "private-index/search-index.json"
        if self.path_service is not None:
            from .artifacts import write_course_artifact_atomic

            write_course_artifact_atomic(self.path_service, course_id, relative_path, payload)
        else:
            private_root.mkdir(parents=True, exist_ok=True)
            (private_root / "search-index.json").write_bytes(payload)

    def _read_fallback_index(self, course_id: str, private_root: Path) -> dict[str, Any]:
        if self.path_service is not None:
            from .artifacts import open_course_artifact_for_read

            with open_course_artifact_for_read(
                self.path_service, course_id, "private-index/search-index.json"
            ) as handle:
                data = handle.read(64 * 1024 * 1024 + 1)
        else:
            data = (private_root / "search-index.json").read_bytes()
        if len(data) > 64 * 1024 * 1024:
            raise InvalidCourseSourceError("Course private index exceeds its size limit")
        payload = json.loads(data.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _fallback_search(
        self,
        course_id: str,
        query: str,
        private_root: Path,
        top_k: int,
    ) -> dict[str, Any]:
        payload = self._read_fallback_index(course_id, private_root)
        terms = tuple(dict.fromkeys(re.findall(r"[\w-]+", query.casefold())))
        ranked: list[tuple[int, dict[str, str]]] = []
        for record in payload.get("records", []):
            if not isinstance(record, dict):
                continue
            text = str(record.get("text") or "")
            score = sum(text.casefold().count(term) for term in terms)
            if score:
                ranked.append(
                    (score, {"filename": str(record.get("filename") or ""), "text": text})
                )
        ranked.sort(key=lambda item: (-item[0], item[1]["filename"]))
        selected = [record for _score, record in ranked[: max(1, min(top_k, 20))]]
        content = "\n\n".join(record["text"] for record in selected)
        return {
            "query": query,
            "answer": content,
            "content": content,
            "provider": "course-private-lexical",
            "sources": [
                {
                    "title": record["filename"],
                    "source": record["filename"],
                    "content": record["text"][:200],
                }
                for record in selected
            ],
        }

    @staticmethod
    def _safe_result(result: Any, query: str) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            raise RuntimeError("Course retrieval returned an invalid result")
        safe = dict(result)
        safe.setdefault("query", query)
        safe.setdefault("answer", safe.get("content", ""))
        safe.setdefault("content", safe.get("answer", ""))
        sources: list[dict[str, Any]] = []
        for source in safe.get("sources", []) or []:
            if not isinstance(source, Mapping):
                continue
            item = dict(source)
            raw_path = str(item.get("source") or "")
            item["source"] = PurePosixPath(raw_path.replace("\\", "/")).name
            sources.append(item)
        safe["sources"] = sources
        return safe

    async def index_sources(
        self,
        *,
        course_id: str,
        source_paths: tuple[Path, ...],
        workspace: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        trusted = self._trusted_source_paths(source_paths, workspace)
        private_root = self._private_index_root(course_id, workspace)
        try:
            service = self._rag_service(private_root)
            result = await self._maybe_await(
                service.initialize(
                    kb_name=course_id,
                    file_paths=[str(path) for path in trusted],
                )
            )
            if result is False:
                raise RuntimeError("Course private indexing did not produce an index")
        except ModuleNotFoundError:
            # The source tree's minimal test environment omits LlamaIndex. The
            # production dependency path above remains authoritative; this
            # bounded fallback keeps Course retrieval restart-safe in stripped
            # installs without pretending metadata is an index.
            self._write_fallback_index(course_id, private_root, trusted)
        if progress:
            progress(len(trusted), len(trusted))

    async def search(
        self,
        *,
        course_id: str,
        query: str,
        workspace: Path,
        top_k: int = 5,
    ) -> Mapping[str, Any]:
        query = query.strip() if isinstance(query, str) else ""
        if not query:
            raise ValueError("Course retrieval query must be non-empty")
        private_root = self._private_index_root(course_id, workspace)
        try:
            service = self._rag_service(private_root)
            result = await self._maybe_await(
                service.search(query=query, kb_name=course_id, top_k=max(1, min(top_k, 20)))
            )
            return self._safe_result(result, query)
        except ModuleNotFoundError:
            return self._fallback_search(course_id, query, private_root, top_k)


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
    "validate_upload_batch_identity",
    "validate_upload_batch",
]
