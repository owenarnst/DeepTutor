"""Transactional SQLite repository for the Course aggregate."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import re
import sqlite3
from typing import Iterable, Iterator
from uuid import UUID, uuid4

from deeptutor.services.path_service import PathService

from .artifacts import (
    InvalidArtifactPathError,
    ensure_course_data_root,
    ensure_course_workspace,
    remove_course_artifact,
    remove_empty_course_directory,
    remove_empty_course_workspace,
    write_course_artifact_atomic,
)
from .models import (
    Course,
    CourseJobStage,
    CourseJobStatus,
    CourseManifest,
    CourseProcessingJob,
    CourseSource,
    CourseStatus,
    ManifestEntry,
    ManifestRole,
    ManifestVisibility,
    Unit,
)
from .source_processing import (
    DefaultCourseIngestionAdapter,
    InvalidCourseSourceError,
    infer_manifest_role,
    validate_upload_batch,
    validate_upload_batch_identity,
)

_LATEST_SCHEMA_VERSION = 5
_REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEFAULT_MAX_COURSES_PER_OWNER = 200
DEFAULT_COURSE_LIST_LIMIT = 50
MAX_COURSE_LIST_LIMIT = 100
MAX_COURSE_LIST_OFFSET = 10_000
_PROCESSING_LEASE_SECONDS = 300


class CourseModeError(Exception):
    """Base class for caller-safe Course Mode failures."""


class InvalidCourseIdentifierError(CourseModeError, ValueError):
    pass


class InvalidRequestKeyError(CourseModeError, ValueError):
    pass


class InvalidCourseInputError(CourseModeError, ValueError):
    pass


class IdempotencyConflictError(CourseModeError):
    pass


class CourseQuotaExceededError(CourseModeError):
    pass


class UnsupportedSchemaVersionError(CourseModeError):
    pass


class InvalidJobRetryError(CourseModeError):
    pass


class ManifestRevisionConflictError(CourseModeError):
    pass


class ManifestApprovalBlockedError(CourseModeError):
    def __init__(self, blockers: Iterable[str]):
        self.blockers = tuple(blockers)
        super().__init__("Manifest review is not complete")


class InvalidManifestStateError(CourseModeError):
    """Raised when a manifest mutation is attempted outside review state."""


class InvalidManifestError(CourseModeError):
    """Raised when a manifest does not cover the accepted source inventory."""


@dataclass(frozen=True)
class CourseInput:
    title: str
    description: str = ""
    unit_title: str = "Unit 1"
    desired_outcome: str = ""
    weekly_minutes: int = 0
    ocw_url: str = ""
    scheduling: str | None = None
    difficulty: str | None = None
    accessibility: str | None = None


@dataclass(frozen=True)
class CreateCourseResult:
    course: Course
    created: bool
    processing_job: CourseProcessingJob | None = None


@dataclass(frozen=True)
class UploadIdentity:
    """Validated bytes and stable identity supplied to Course creation."""

    original_filename: str
    display_filename: str
    content: bytes
    content_hash: str
    extracted_text: str


@dataclass(frozen=True)
class CourseImportResult:
    course: Course
    processing_job: CourseProcessingJob
    created: bool


@dataclass(frozen=True)
class CoursePage:
    courses: tuple[Course, ...]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.courses) < self.total

    @property
    def next_offset(self) -> int | None:
        if not self.has_more:
            return None
        return self.offset + len(self.courses)


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _normalize_input(value: CourseInput, *, require_source: bool = False) -> CourseInput:
    if not isinstance(value, CourseInput):
        raise InvalidCourseInputError("Course input is invalid")
    for field_name in ("title", "description", "unit_title", "desired_outcome", "ocw_url"):
        if not isinstance(getattr(value, field_name), str):
            raise InvalidCourseInputError("Course input is invalid")
    for field_name in ("scheduling", "difficulty", "accessibility"):
        optional_value = getattr(value, field_name)
        if optional_value is not None and not isinstance(optional_value, str):
            raise InvalidCourseInputError("Optional Course input is invalid")
    if isinstance(value.weekly_minutes, bool) or not isinstance(value.weekly_minutes, int):
        raise InvalidCourseInputError("Weekly minutes must be an integer")
    weekly_minutes = value.weekly_minutes
    normalized = CourseInput(
        title=_normalize_text(value.title),
        description=_normalize_text(value.description),
        unit_title=_normalize_text(value.unit_title),
        desired_outcome=_normalize_text(value.desired_outcome),
        weekly_minutes=weekly_minutes,
        ocw_url=value.ocw_url.strip() if isinstance(value.ocw_url, str) else "",
        scheduling=_normalize_text(value.scheduling) if value.scheduling else None,
        difficulty=_normalize_text(value.difficulty) if value.difficulty else None,
        accessibility=_normalize_text(value.accessibility) if value.accessibility else None,
    )
    if not normalized.title or len(normalized.title) > 200:
        raise InvalidCourseInputError("Course title must be between 1 and 200 characters")
    if len(normalized.description) > 2000:
        raise InvalidCourseInputError("Course description must be at most 2000 characters")
    if not normalized.unit_title or len(normalized.unit_title) > 200:
        raise InvalidCourseInputError("Unit title must be between 1 and 200 characters")
    if require_source and not normalized.desired_outcome:
        raise InvalidCourseInputError("Desired outcome is required")
    if len(normalized.desired_outcome) > 2000:
        raise InvalidCourseInputError("Desired outcome must be at most 2000 characters")
    if require_source and not 1 <= normalized.weekly_minutes <= 10_080:
        raise InvalidCourseInputError("Weekly minutes must be between 1 and 10080")
    if normalized.weekly_minutes < 0 or normalized.weekly_minutes > 10_080:
        raise InvalidCourseInputError("Weekly minutes must be between 1 and 10080")
    if require_source or normalized.ocw_url:
        from .source_processing import normalize_ocw_url

        try:
            normalized = CourseInput(
                **{
                    **normalized.__dict__,
                    "ocw_url": normalize_ocw_url(normalized.ocw_url),
                }
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidCourseInputError):
                raise
            raise InvalidCourseInputError("OCW URL is invalid") from exc
    for label, optional in (
        ("Scheduling", normalized.scheduling),
        ("Difficulty", normalized.difficulty),
        ("Accessibility", normalized.accessibility),
    ):
        if optional and len(optional) > 2000:
            raise InvalidCourseInputError(f"{label} input must be at most 2000 characters")
    return normalized


def _fingerprint(value: CourseInput) -> str:
    payload = json.dumps(
        {
            "description": value.description,
            "title": value.title,
            "unit_title": value.unit_title,
            "desired_outcome": value.desired_outcome,
            "weekly_minutes": value.weekly_minutes,
            "ocw_url": value.ocw_url,
            "scheduling": value.scheduling,
            "difficulty": value.difficulty,
            "accessibility": value.accessibility,
            "version": 2,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legacy_draft_fingerprint(value: CourseInput) -> str:
    """Keep OWE-6 source-less idempotency rows replayable after migration."""
    payload = json.dumps(
        {
            "description": value.description,
            "title": value.title,
            "unit_title": value.unit_title,
            "version": 1,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _import_fingerprint(value: CourseInput, upload_identities: tuple[str, ...]) -> str:
    payload = {
        "course": _fingerprint(value),
        "uploads": upload_identities,
        "version": 1,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _validate_course_id(course_id: str) -> str:
    try:
        parsed = UUID(course_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidCourseIdentifierError("Invalid course id") from exc
    canonical = str(parsed)
    if course_id != canonical:
        raise InvalidCourseIdentifierError("Invalid course id")
    return canonical


def _validate_uuid(value: str, label: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidCourseIdentifierError(f"Invalid {label}") from exc
    canonical = str(parsed)
    if value != canonical:
        raise InvalidCourseIdentifierError(f"Invalid {label}")
    return canonical


def _manifest_blockers(entries: Iterable[ManifestEntry]) -> tuple[str, ...]:
    blockers: list[str] = []
    for entry in entries:
        if entry.role is ManifestRole.UNKNOWN:
            blockers.append("unknown_role")
        if entry.suspected_solution and (
            not entry.role_confirmed or not entry.visibility_confirmed
        ):
            blockers.append("suspected_solution_confirmation")
    return tuple(dict.fromkeys(blockers))


def _execute_migration_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute static DDL statements without ``executescript``'s implicit commit."""
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        statement = "\n".join(buffer)
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            buffer.clear()
    if "".join(buffer).strip():
        raise RuntimeError("Incomplete Course Mode migration statement")


class CourseRepository:
    """One repository instance serves exactly one server-derived user scope.

    SQLite accepts pathname databases rather than a pre-opened directory
    handle. The path is safe because its ancestors are created no-follow under
    the server-private Course root, which is absent from runner mounts and is
    writable only by the trusted application process.
    """

    def __init__(
        self,
        path_service: PathService,
        *,
        owner_scope: str,
        max_courses: int = DEFAULT_MAX_COURSES_PER_OWNER,
        ingestion_adapter: object | None = None,
    ):
        if not owner_scope:
            raise ValueError("owner_scope is required")
        if not 1 <= max_courses <= 10_000:
            raise ValueError("max_courses must be between 1 and 10000")
        self.path_service = path_service
        self.owner_scope = owner_scope
        self.max_courses = max_courses
        self.db_path = path_service.get_course_mode_db()
        self.ingestion_adapter = ingestion_adapter or DefaultCourseIngestionAdapter(path_service)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        ensure_course_data_root(self.path_service)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == _LATEST_SCHEMA_VERSION:
                return
            if version > _LATEST_SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"Course Mode database schema {version} is newer than supported"
                )

            # Schema v4 replaces mutually-referencing tables. SQLite cannot
            # toggle foreign-key enforcement inside a transaction, so disable
            # it before taking the migration lease and prove the rebuilt graph
            # with ``foreign_key_check`` before commit.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have completed migration while this
                # connection waited for the write lease.
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version > _LATEST_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(
                        f"Course Mode database schema {version} is newer than supported"
                    )
                if version < 1:
                    self._migrate_to_v1(conn)
                    conn.execute("PRAGMA user_version = 1")
                    version = 1
                if version < 2:
                    self._migrate_to_v2(conn)
                    conn.execute("PRAGMA user_version = 2")
                    version = 2
                if version < 3:
                    self._migrate_to_v3(conn)
                    conn.execute("PRAGMA user_version = 3")
                    version = 3
                if version < 4:
                    self._migrate_to_v4(conn)
                    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise sqlite3.IntegrityError(
                            "Course Mode migration produced invalid foreign keys"
                        )
                    conn.execute("PRAGMA user_version = 4")
                    version = 4
                if version < 5:
                    self._migrate_to_v5(conn)
                    conn.execute("PRAGMA user_version = 5")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_to_v1(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS courses (
                id TEXT PRIMARY KEY,
                owner_scope TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_courses_owner_updated
            ON courses(owner_scope, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS units (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL UNIQUE REFERENCES courses(id) ON DELETE RESTRICT,
                title TEXT NOT NULL,
                position INTEGER NOT NULL CHECK(position >= 0),
                UNIQUE(course_id, position)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                owner_scope TEXT NOT NULL,
                request_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_scope, request_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS course_audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS course_audit_events_no_update
            BEFORE UPDATE ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'course audit events are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS course_audit_events_no_delete
            BEFORE DELETE ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'course audit events are append-only'); END
            """,
        )
        for statement in statements:
            conn.execute(statement)

    @staticmethod
    def _migrate_to_v2(conn: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE INDEX IF NOT EXISTS idx_courses_owner_updated
            ON courses(owner_scope, updated_at DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS artifact_references (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
                unit_id TEXT REFERENCES units(id) ON DELETE RESTRICT,
                kind TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(course_id, relative_path),
                CHECK(substr(relative_path, 1, 1) != '/'),
                CHECK(instr(relative_path, '..') = 0)
            )
            """,
            # Some pre-versioned/experimental databases already carry the v1
            # tables but not their triggers. Reassert these invariants as part
            # of the additive migration rather than trusting schema history.
            """
            CREATE TRIGGER IF NOT EXISTS course_audit_events_no_update
            BEFORE UPDATE ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'course audit events are append-only'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS course_audit_events_no_delete
            BEFORE DELETE ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'course audit events are append-only'); END
            """,
        )
        for statement in statements:
            conn.execute(statement)

    @staticmethod
    def _migrate_to_v3(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_units_course_id_id ON units(course_id, id)"
        )
        conn.execute("ALTER TABLE artifact_references RENAME TO artifact_references_v2")
        conn.execute(
            """
            CREATE TABLE artifact_references (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
                unit_id TEXT,
                kind TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(course_id, relative_path),
                FOREIGN KEY(course_id, unit_id)
                    REFERENCES units(course_id, id) ON DELETE RESTRICT,
                CHECK(length(relative_path) > 0),
                CHECK(instr(relative_path, char(0)) = 0),
                CHECK(instr(relative_path, '\\') = 0),
                CHECK(substr(relative_path, 1, 1) != '/'),
                CHECK(NOT (
                    substr(relative_path, 1, 1) GLOB '[A-Za-z]'
                    AND substr(relative_path, 2, 1) = ':'
                )),
                CHECK(relative_path NOT IN ('.', '..')),
                CHECK(instr('/' || relative_path || '/', '/./') = 0),
                CHECK(instr('/' || relative_path || '/', '/../') = 0),
                CHECK(instr(relative_path, '//') = 0),
                CHECK(substr(relative_path, -1, 1) != '/')
            )
            """
        )
        conn.execute(
            """
            INSERT INTO artifact_references(
                id, course_id, unit_id, kind, relative_path, content_hash, created_at
            )
            SELECT id, course_id, unit_id, kind, relative_path, content_hash, created_at
            FROM artifact_references_v2
            """
        )
        conn.execute("DROP TABLE artifact_references_v2")

    @staticmethod
    def _migrate_to_v4(conn: sqlite3.Connection) -> None:
        invalid = conn.execute(
            """
            SELECT c.id
            FROM courses AS c
            LEFT JOIN units AS u ON u.course_id = c.id
            GROUP BY c.id
            HAVING COUNT(u.id) != 1
            LIMIT 1
            """
        ).fetchone()
        if invalid is not None:
            raise sqlite3.IntegrityError(
                "Every Course must have exactly one Unit before schema migration"
            )

        _execute_migration_script(
            conn,
            """
            CREATE TABLE courses_v4 (
                id TEXT PRIMARY KEY,
                owner_scope TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                primary_unit_id TEXT NOT NULL,
                UNIQUE(id, primary_unit_id),
                FOREIGN KEY(id, primary_unit_id)
                    REFERENCES units_v4(course_id, id) ON DELETE RESTRICT
            );

            CREATE TABLE units_v4 (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                position INTEGER NOT NULL CHECK(position = 0),
                UNIQUE(course_id, position),
                UNIQUE(course_id, id),
                FOREIGN KEY(course_id) REFERENCES courses_v4(id) ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED
            );

            CREATE TABLE idempotency_keys_v4 (
                owner_scope TEXT NOT NULL,
                request_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                course_id TEXT NOT NULL REFERENCES courses_v4(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_scope, request_key)
            );

            CREATE TABLE course_audit_events_v4 (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT NOT NULL REFERENCES courses_v4(id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );

            CREATE TABLE artifact_references_v4 (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses_v4(id) ON DELETE RESTRICT,
                unit_id TEXT,
                kind TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(course_id, relative_path),
                FOREIGN KEY(course_id, unit_id)
                    REFERENCES units_v4(course_id, id) ON DELETE RESTRICT,
                CHECK(length(relative_path) > 0),
                CHECK(instr(relative_path, char(0)) = 0),
                CHECK(instr(relative_path, '\\') = 0),
                CHECK(substr(relative_path, 1, 1) != '/'),
                CHECK(NOT (
                    substr(relative_path, 1, 1) GLOB '[A-Za-z]'
                    AND substr(relative_path, 2, 1) = ':'
                )),
                CHECK(relative_path NOT IN ('.', '..')),
                CHECK(instr('/' || relative_path || '/', '/./') = 0),
                CHECK(instr('/' || relative_path || '/', '/../') = 0),
                CHECK(instr(relative_path, '//') = 0),
                CHECK(substr(relative_path, -1, 1) != '/')
            );

            INSERT INTO courses_v4(
                id, owner_scope, title, description, status, workspace_ref,
                created_at, updated_at, primary_unit_id
            )
            SELECT
                c.id, c.owner_scope, c.title, c.description, c.status, c.workspace_ref,
                c.created_at, c.updated_at, u.id
            FROM courses AS c
            JOIN units AS u ON u.course_id = c.id;

            INSERT INTO units_v4(id, course_id, title, position)
            SELECT id, course_id, title, position FROM units;

            INSERT INTO idempotency_keys_v4
            SELECT * FROM idempotency_keys;

            INSERT INTO course_audit_events_v4
            SELECT * FROM course_audit_events;

            INSERT INTO artifact_references_v4
            SELECT * FROM artifact_references;

            DROP TRIGGER IF EXISTS course_audit_events_no_update;
            DROP TRIGGER IF EXISTS course_audit_events_no_delete;
            DROP TABLE artifact_references;
            DROP TABLE course_audit_events;
            DROP TABLE idempotency_keys;
            DROP TABLE units;
            DROP TABLE courses;

            ALTER TABLE courses_v4 RENAME TO courses;
            ALTER TABLE units_v4 RENAME TO units;
            ALTER TABLE idempotency_keys_v4 RENAME TO idempotency_keys;
            ALTER TABLE course_audit_events_v4 RENAME TO course_audit_events;
            ALTER TABLE artifact_references_v4 RENAME TO artifact_references;

            CREATE INDEX idx_courses_owner_updated
            ON courses(owner_scope, updated_at DESC);
            CREATE UNIQUE INDEX idx_units_course_id_id ON units(course_id, id);

            CREATE TRIGGER course_audit_events_no_update
            BEFORE UPDATE ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'course audit events are append-only'); END;

            CREATE TRIGGER course_audit_events_no_delete
            BEFORE DELETE ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'course audit events are append-only'); END;
            """,
        )

    @staticmethod
    def _migrate_to_v5(conn: sqlite3.Connection) -> None:
        """Add Course-owned source, job, and manifest state to OWE-6."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(courses)").fetchall()}
        additions = (
            ("desired_outcome", "TEXT NOT NULL DEFAULT ''"),
            ("weekly_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("ocw_url", "TEXT NOT NULL DEFAULT ''"),
            ("scheduling_json", "TEXT"),
            ("difficulty", "TEXT"),
            ("accessibility", "TEXT"),
        )
        for name, declaration in additions:
            if name not in columns:
                conn.execute(f"ALTER TABLE courses ADD COLUMN {name} {declaration}")
        _execute_migration_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS course_processing_jobs (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL UNIQUE REFERENCES courses(id) ON DELETE RESTRICT,
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'source_processing', 'awaiting_manifest_review', 'completed', 'failed'
                )),
                stage TEXT NOT NULL CHECK(stage IN (
                    'queued', 'source_processing', 'awaiting_manifest_review', 'completed'
                )),
                failed_stage TEXT CHECK(failed_stage IS NULL OR failed_stage IN (
                    'queued', 'source_processing', 'awaiting_manifest_review', 'completed'
                )),
                error_code TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                manifest_revision INTEGER NOT NULL DEFAULT 0 CHECK(manifest_revision >= 0),
                source_indexed INTEGER NOT NULL DEFAULT 0 CHECK(source_indexed IN (0, 1)),
                lease_token TEXT,
                lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_course_jobs_course_status
            ON course_processing_jobs(course_id, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS course_sources (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
                unit_id TEXT,
                original_filename TEXT NOT NULL,
                display_filename TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                upload_identity TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                extracted_chars INTEGER NOT NULL DEFAULT 0 CHECK(extracted_chars >= 0),
                created_at TEXT NOT NULL,
                UNIQUE(course_id, upload_identity),
                UNIQUE(course_id, relative_path),
                UNIQUE(course_id, id),
                FOREIGN KEY(course_id, unit_id)
                    REFERENCES units(course_id, id) ON DELETE RESTRICT,
                CHECK(length(original_filename) > 0),
                CHECK(length(display_filename) > 0),
                CHECK(length(relative_path) > 0),
                CHECK(substr(relative_path, 1, 1) != '/'),
                CHECK(instr(relative_path, '\\') = 0),
                CHECK(instr('/' || relative_path || '/', '/../') = 0)
            );

            CREATE INDEX IF NOT EXISTS idx_course_sources_course
            ON course_sources(course_id, created_at, id);

            CREATE TABLE IF NOT EXISTS course_manifest_entries (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
                source_id TEXT NOT NULL UNIQUE REFERENCES course_sources(id) ON DELETE RESTRICT,
                role TEXT NOT NULL CHECK(role IN (
                    'syllabus', 'lecture_note', 'reading', 'assignment',
                    'solution', 'grading_resource', 'unknown'
                )),
                visibility TEXT NOT NULL CHECK(visibility IN ('learner_visible', 'instructor_only')),
                suspected_solution INTEGER NOT NULL DEFAULT 0 CHECK(suspected_solution IN (0, 1)),
                role_confirmed INTEGER NOT NULL DEFAULT 1 CHECK(role_confirmed IN (0, 1)),
                visibility_confirmed INTEGER NOT NULL DEFAULT 1 CHECK(visibility_confirmed IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(course_id, id),
                FOREIGN KEY(course_id, source_id)
                    REFERENCES course_sources(course_id, id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_course_manifest_course
            ON course_manifest_entries(course_id, created_at, id);
            """,
        )
        job_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(course_processing_jobs)").fetchall()
        }
        for name, declaration in (("lease_token", "TEXT"), ("lease_until", "TEXT")):
            if name not in job_columns:
                conn.execute(f"ALTER TABLE course_processing_jobs ADD COLUMN {name} {declaration}")

    def create_draft(self, request_key: str, course_input: CourseInput) -> CreateCourseResult:
        if not _REQUEST_KEY_RE.fullmatch(request_key):
            raise InvalidRequestKeyError("Idempotency key must be 1-128 URL-safe characters")
        normalized = _normalize_input(course_input)
        request_fingerprint = (
            _legacy_draft_fingerprint(normalized)
            if not normalized.desired_outcome
            and normalized.weekly_minutes == 0
            and not normalized.ocw_url
            and normalized.scheduling is None
            and normalized.difficulty is None
            and normalized.accessibility is None
            else _fingerprint(normalized)
        )
        self.initialize()

        workspace_created = False
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute(
                    """
                    SELECT request_fingerprint, course_id
                    FROM idempotency_keys
                    WHERE owner_scope = ? AND request_key = ?
                    """,
                    (self.owner_scope, request_key),
                ).fetchone()
                if prior is not None:
                    if prior["request_fingerprint"] != request_fingerprint:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used with different course input"
                        )
                    course = self._get_with_connection(conn, prior["course_id"])
                    if course is None:  # Defensive: FK should make this impossible.
                        raise RuntimeError("Idempotency record references a missing course")
                    conn.commit()
                    return CreateCourseResult(course=course, created=False)

                current_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM courses WHERE owner_scope = ?",
                        (self.owner_scope,),
                    ).fetchone()[0]
                )
                if current_count >= self.max_courses:
                    raise CourseQuotaExceededError(
                        f"Course limit reached ({self.max_courses} per owner)"
                    )

                course_id = str(uuid4())
                unit_id = str(uuid4())
                now = datetime.now(timezone.utc).isoformat()
                workspace_ref = f"courses/{course_id}"
                conn.execute(
                    "INSERT INTO units(id, course_id, title, position) VALUES (?, ?, ?, 0)",
                    (unit_id, course_id, normalized.unit_title),
                )
                conn.execute(
                    """
                    INSERT INTO courses(
                        id, owner_scope, title, description, status, workspace_ref,
                        created_at, updated_at, primary_unit_id, desired_outcome,
                        weekly_minutes, ocw_url, scheduling_json, difficulty, accessibility
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course_id,
                        self.owner_scope,
                        normalized.title,
                        normalized.description,
                        CourseStatus.DRAFT.value,
                        workspace_ref,
                        now,
                        now,
                        unit_id,
                        normalized.desired_outcome,
                        normalized.weekly_minutes,
                        normalized.ocw_url,
                        normalized.scheduling,
                        normalized.difficulty,
                        normalized.accessibility,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO idempotency_keys(
                        owner_scope, request_key, request_fingerprint, course_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.owner_scope, request_key, request_fingerprint, course_id, now),
                )

                workspace_created = ensure_course_workspace(self.path_service, course_id)

                conn.execute(
                    """
                    INSERT INTO course_audit_events(
                        course_id, event_type, from_status, to_status, payload_json, occurred_at
                    ) VALUES (?, 'course.created', NULL, ?, ?, ?)
                    """,
                    (
                        course_id,
                        CourseStatus.DRAFT.value,
                        json.dumps(
                            {"course_id": course_id, "unit_id": unit_id},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                course = self._get_with_connection(conn, course_id)
                if course is None:
                    raise RuntimeError("Created course could not be read")
                conn.commit()
                return CreateCourseResult(course=course, created=True)
            except Exception:
                conn.rollback()
                if workspace_created:
                    try:
                        remove_empty_course_workspace(self.path_service, course_id)
                    except (OSError, InvalidArtifactPathError):
                        pass
                raise

    def create_import(
        self,
        request_key: str,
        course_input: CourseInput,
        uploads: Iterable[tuple[str, bytes]],
    ) -> CourseImportResult:
        """Create one strict Course aggregate and durably queue its sources.

        Every upload is parsed before the transaction mutates Course storage.
        The transaction then owns the database rows and compensates all files
        if any row, audit, or filesystem operation fails.
        """
        if not _REQUEST_KEY_RE.fullmatch(request_key):
            raise InvalidRequestKeyError("Idempotency key must be 1-128 URL-safe characters")
        normalized = _normalize_input(course_input, require_source=True)
        uploads = tuple(uploads)
        cheap_identities = validate_upload_batch_identity(uploads)
        upload_identities = tuple(
            sorted(
                f"{display_name}\x00{content_hash}\x00{size}"
                for _original_name, display_name, content_hash, _content, size in cheap_identities
            )
        )
        request_fingerprint = _import_fingerprint(normalized, upload_identities)
        self.initialize()

        # Check the authoritative replay row before expensive parser/extractor
        # work. A response-loss retry still performs cheap identity validation,
        # but a known accepted upload does not parse Office/PDF/text again.
        with self._connect() as conn:
            conn.execute("BEGIN")
            prior = conn.execute(
                """
                SELECT request_fingerprint, course_id
                FROM idempotency_keys
                WHERE owner_scope = ? AND request_key = ?
                """,
                (self.owner_scope, request_key),
            ).fetchone()
            if prior is not None:
                if prior["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflictError(
                        "Idempotency key was already used with different course input"
                    )
                course = self._get_with_connection(conn, prior["course_id"])
                job = self._job_with_connection(conn, course_id=prior["course_id"])
                if course is None or job is None:
                    raise RuntimeError("Idempotency record references incomplete Course state")
                conn.commit()
                return CourseImportResult(course=course, processing_job=job, created=False)
            current_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM courses WHERE owner_scope = ?",
                    (self.owner_scope,),
                ).fetchone()[0]
            )
            if current_count >= self.max_courses:
                raise CourseQuotaExceededError(
                    f"Course limit reached ({self.max_courses} per owner)"
                )
            conn.commit()

        try:
            prepared = validate_upload_batch(uploads)
        except InvalidCourseSourceError:
            raise

        workspace_created = False
        written_paths: list[str] = []
        course_id = ""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute(
                    """
                    SELECT request_fingerprint, course_id
                    FROM idempotency_keys
                    WHERE owner_scope = ? AND request_key = ?
                    """,
                    (self.owner_scope, request_key),
                ).fetchone()
                if prior is not None:
                    if prior["request_fingerprint"] != request_fingerprint:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used with different course input"
                        )
                    course_id = prior["course_id"]
                    course = self._get_with_connection(conn, course_id)
                    job = self._job_with_connection(conn, course_id=course_id)
                    if course is None or job is None:
                        raise RuntimeError("Idempotency record references incomplete Course state")
                    conn.commit()
                    return CourseImportResult(course=course, processing_job=job, created=False)

                current_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM courses WHERE owner_scope = ?",
                        (self.owner_scope,),
                    ).fetchone()[0]
                )
                if current_count >= self.max_courses:
                    raise CourseQuotaExceededError(
                        f"Course limit reached ({self.max_courses} per owner)"
                    )

                course_id = str(uuid4())
                unit_id = str(uuid4())
                job_id = str(uuid4())
                now = datetime.now(timezone.utc).isoformat()
                workspace_ref = f"courses/{course_id}"
                workspace_created = ensure_course_workspace(self.path_service, course_id)

                # Establish the aggregate parent rows before child source and
                # artifact rows. Their one-unit relationship is deferred by
                # the OWE-6 v4 schema and checked again at commit.
                conn.execute(
                    """
                    INSERT INTO units(id, course_id, title, position) VALUES (?, ?, ?, 0)
                    """,
                    (unit_id, course_id, normalized.unit_title),
                )
                conn.execute(
                    """
                    INSERT INTO courses(
                        id, owner_scope, title, description, status, workspace_ref,
                        created_at, updated_at, primary_unit_id, desired_outcome,
                        weekly_minutes, ocw_url, scheduling_json, difficulty, accessibility
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course_id,
                        self.owner_scope,
                        normalized.title,
                        normalized.description,
                        CourseStatus.DRAFT.value,
                        workspace_ref,
                        now,
                        now,
                        unit_id,
                        normalized.desired_outcome,
                        normalized.weekly_minutes,
                        normalized.ocw_url,
                        normalized.scheduling,
                        normalized.difficulty,
                        normalized.accessibility,
                    ),
                )

                # Store only sanitized POSIX-relative names. All writes go via
                # operation-owned no-follow descriptors; the API never exposes
                # these paths to a caller.
                for original_name, display_name, content_hash, content, text, size in prepared:
                    relative_path = f"sources/{display_name}"
                    write_course_artifact_atomic(
                        self.path_service,
                        course_id,
                        relative_path,
                        content,
                    )
                    written_paths.append(relative_path)
                    source_id = str(uuid4())
                    upload_identity = hashlib.sha256(
                        f"{display_name}\x00{content_hash}\x00{size}".encode("utf-8")
                    ).hexdigest()
                    conn.execute(
                        """
                        INSERT INTO course_sources(
                            id, course_id, unit_id, original_filename, display_filename,
                            relative_path, content_hash, upload_identity, size_bytes,
                            extracted_chars, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            course_id,
                            unit_id,
                            original_name,
                            display_name,
                            relative_path,
                            content_hash,
                            upload_identity,
                            size,
                            len(text),
                            now,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO artifact_references(
                            id, course_id, unit_id, kind, relative_path, content_hash, created_at
                        ) VALUES (?, ?, ?, 'course_source', ?, ?, ?)
                        """,
                        (str(uuid4()), course_id, unit_id, relative_path, content_hash, now),
                    )

                conn.execute(
                    """
                    INSERT INTO course_processing_jobs(
                        id, course_id, status, stage, failed_stage, error_code,
                        attempt_count, manifest_revision, source_indexed, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, 0, ?, ?)
                    """,
                    (
                        job_id,
                        course_id,
                        CourseJobStatus.QUEUED.value,
                        CourseJobStage.QUEUED.value,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO idempotency_keys(
                        owner_scope, request_key, request_fingerprint, course_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.owner_scope, request_key, request_fingerprint, course_id, now),
                )
                conn.execute(
                    """
                    INSERT INTO course_audit_events(
                        course_id, event_type, from_status, to_status, payload_json, occurred_at
                    ) VALUES (?, 'course.import_queued', NULL, ?, ?, ?)
                    """,
                    (
                        course_id,
                        CourseStatus.DRAFT.value,
                        json.dumps(
                            {
                                "course_id": course_id,
                                "job_id": job_id,
                                "source_count": len(prepared),
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                course = self._get_with_connection(conn, course_id)
                job = self._job_with_connection(conn, course_id=course_id)
                if course is None or job is None:
                    raise RuntimeError("Created Course import could not be read")
                conn.commit()
                return CourseImportResult(course=course, processing_job=job, created=True)
            except Exception:
                conn.rollback()
                for relative_path in written_paths:
                    try:
                        remove_course_artifact(self.path_service, course_id, relative_path)
                    except (OSError, InvalidArtifactPathError):
                        pass
                if workspace_created and course_id:
                    try:
                        remove_empty_course_directory(self.path_service, course_id, "sources")
                    except (OSError, InvalidArtifactPathError):
                        pass
                    try:
                        remove_empty_course_workspace(self.path_service, course_id)
                    except (OSError, InvalidArtifactPathError):
                        pass
                raise

    def process_import(self, job_id: str) -> CourseProcessingJob:
        """Run the queued source stage and persist a reviewable manifest."""
        self.initialize()
        job_id = _validate_uuid(job_id, "job id")
        with self._connect() as conn:
            job_row = conn.execute(
                """
                SELECT j.*, c.owner_scope
                FROM course_processing_jobs AS j
                JOIN courses AS c ON c.id = j.course_id
                WHERE j.id = ? AND c.owner_scope = ?
                """,
                (job_id, self.owner_scope),
            ).fetchone()
            if job_row is None:
                raise InvalidCourseIdentifierError("Processing job not found")
            if job_row["status"] in {
                CourseJobStatus.AWAITING_MANIFEST_REVIEW.value,
                CourseJobStatus.COMPLETED.value,
            }:
                return self._job_from_row(job_row)
            if job_row["status"] == CourseJobStatus.FAILED.value:
                raise InvalidJobRetryError(
                    "Processing job failed; retry the failed stage explicitly"
                )
            now = datetime.now(timezone.utc).isoformat()
            lease_until = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + _PROCESSING_LEASE_SECONDS,
                timezone.utc,
            ).isoformat()
            lease_token = uuid4().hex
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM course_processing_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError("Processing job disappeared")
            if (
                current["status"] == CourseJobStatus.SOURCE_PROCESSING.value
                and current["lease_until"]
                and current["lease_until"] > now
            ):
                conn.rollback()
                return self._job_from_row(current)
            is_queued = current["status"] == CourseJobStatus.QUEUED.value
            claimed = conn.execute(
                """
                UPDATE course_processing_jobs
                SET status = ?, stage = ?, attempt_count = attempt_count + ?,
                    failed_stage = NULL, error_code = NULL,
                    lease_token = ?, lease_until = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    CourseJobStatus.SOURCE_PROCESSING.value,
                    CourseJobStage.SOURCE_PROCESSING.value,
                    int(is_queued),
                    lease_token,
                    lease_until,
                    now,
                    job_id,
                    current["status"],
                ),
            )
            if claimed.rowcount != 1:
                conn.rollback()
                current = conn.execute(
                    "SELECT * FROM course_processing_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if current is None:
                    raise RuntimeError("Processing job disappeared")
                return self._job_from_row(current)
            conn.commit()

        course_id = job_row["course_id"]
        if not bool(current["source_indexed"]):
            sources = self.list_sources(course_id)
            source_paths = tuple(
                self.path_service.get_course_workspace(course_id) / source.relative_path
                for source in sources
            )
            try:
                indexer = self.ingestion_adapter
                index_sources = getattr(indexer, "index_sources", None)
                if not callable(index_sources):
                    raise RuntimeError("Course ingestion adapter is unavailable")
                indexing_result = index_sources(
                    course_id=course_id,
                    source_paths=source_paths,
                    workspace=self.path_service.get_course_workspace(course_id),
                )
                if inspect.isawaitable(indexing_result):
                    # Repository work runs in a worker thread at the API
                    # boundary, so an injected async RAG adapter can reuse
                    # its native API without blocking the event loop.
                    import asyncio

                    asyncio.run(indexing_result)
            except Exception:
                return self._mark_job_failed(
                    job_id,
                    CourseJobStage.SOURCE_PROCESSING,
                    "source_processing_failed",
                    lease_token=lease_token,
                )

            # Commit the completed indexing stage separately. If the process
            # dies while manifest rows are being written, recovery can skip
            # this already-successful stage and resume at manifest creation.
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE course_processing_jobs
                    SET source_indexed = 1, updated_at = ?
                    WHERE id = ? AND status = ? AND lease_token = ?
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        job_id,
                        CourseJobStatus.SOURCE_PROCESSING.value,
                        lease_token,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    conn.rollback()
                    current_row = conn.execute(
                        "SELECT * FROM course_processing_jobs WHERE id = ?", (job_id,)
                    ).fetchone()
                    if current_row is None:
                        raise RuntimeError("Processing job disappeared")
                    return self._job_from_row(current_row)
                conn.commit()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            current_job = conn.execute(
                """
                SELECT * FROM course_processing_jobs
                WHERE id = ? AND course_id = ? AND status = ? AND lease_token = ?
                """,
                (job_id, course_id, CourseJobStatus.SOURCE_PROCESSING.value, lease_token),
            ).fetchone()
            if current_job is None:
                conn.rollback()
                current = conn.execute(
                    "SELECT * FROM course_processing_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if current is None:
                    raise RuntimeError("Processing job disappeared")
                return self._job_from_row(current)
            existing_source_ids = {
                row["source_id"]
                for row in conn.execute(
                    "SELECT source_id FROM course_manifest_entries WHERE course_id = ?",
                    (course_id,),
                ).fetchall()
            }
            for source_row in conn.execute(
                """
                SELECT id, original_filename, display_filename
                FROM course_sources WHERE course_id = ? ORDER BY created_at, id
                """,
                (course_id,),
            ).fetchall():
                if source_row["id"] in existing_source_ids:
                    continue
                proposal = infer_manifest_role(source_row["display_filename"])
                conn.execute(
                    """
                    INSERT INTO course_manifest_entries(
                        id, course_id, source_id, role, visibility, suspected_solution,
                        role_confirmed, visibility_confirmed, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        course_id,
                        source_row["id"],
                        proposal.role.value,
                        proposal.visibility.value,
                        int(proposal.suspected_solution),
                        int(proposal.role_confirmed),
                        int(proposal.visibility_confirmed),
                        now,
                        now,
                    ),
                )
            revision = int(current_job["manifest_revision"] or 0) + 1
            conn.execute(
                """
                UPDATE course_processing_jobs
                SET status = ?, stage = ?, source_indexed = 1,
                    manifest_revision = ?, failed_stage = NULL, error_code = NULL,
                    lease_token = NULL, lease_until = NULL, updated_at = ?
                WHERE id = ? AND lease_token = ?
                """,
                (
                    CourseJobStatus.AWAITING_MANIFEST_REVIEW.value,
                    CourseJobStage.MANIFEST_REVIEW.value,
                    revision,
                    now,
                    job_id,
                    lease_token,
                ),
            )
            conn.execute(
                """
                INSERT INTO course_audit_events(
                    course_id, event_type, from_status, to_status, payload_json, occurred_at
                ) VALUES (?, 'course.manifest_ready', ?, ?, ?, ?)
                """,
                (
                    course_id,
                    CourseStatus.DRAFT.value,
                    CourseStatus.DRAFT.value,
                    json.dumps(
                        {"job_id": job_id, "manifest_revision": revision},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM course_processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Processing job disappeared")
            return self._job_from_row(row)

    def retry_import(self, job_id: str) -> CourseProcessingJob:
        """Retry exactly one failed source-processing stage."""
        self.initialize()
        canonical = _validate_uuid(job_id, "job id")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT j.* FROM course_processing_jobs AS j
                JOIN courses AS c ON c.id = j.course_id
                WHERE j.id = ? AND c.owner_scope = ?
                """,
                (canonical, self.owner_scope),
            ).fetchone()
            if row is None:
                raise InvalidCourseIdentifierError("Processing job not found")
            if row["status"] == CourseJobStatus.SOURCE_PROCESSING.value:
                now = datetime.now(timezone.utc).isoformat()
                if row["lease_until"] and row["lease_until"] > now:
                    raise InvalidJobRetryError("Processing job is still active")
                conn.commit()
            elif row["status"] == CourseJobStatus.QUEUED.value:
                # A committed queued row is the durable hand-off point. It can
                # remain after a worker/process crash before the API's
                # best-effort inline runner starts. Release the short
                # transaction before claiming it so concurrent public
                # recovery requests converge through process_import's lease
                # claim instead of duplicating indexing or manifest rows.
                conn.commit()
            elif row["status"] != CourseJobStatus.FAILED.value:
                raise InvalidJobRetryError("Only failed processing jobs can be retried")
            elif row["failed_stage"] != CourseJobStage.SOURCE_PROCESSING.value:
                raise InvalidJobRetryError("This processing stage cannot be retried")
            else:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    UPDATE course_processing_jobs
                    SET status = ?, stage = ?, failed_stage = NULL, error_code = NULL,
                        lease_token = NULL, lease_until = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (CourseJobStatus.QUEUED.value, CourseJobStage.QUEUED.value, now, canonical),
                )
                conn.commit()
        return self.process_import(canonical)

    def _mark_job_failed(
        self,
        job_id: str,
        failed_stage: CourseJobStage,
        error_code: str,
        *,
        lease_token: str | None = None,
    ) -> CourseProcessingJob:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            where = "WHERE id = ?"
            parameters: list[object] = [
                CourseJobStatus.FAILED.value,
                CourseJobStage(failed_stage.value).value,
                failed_stage.value,
                error_code,
                now,
                job_id,
            ]
            if lease_token is not None:
                where += " AND lease_token = ?"
                parameters.append(lease_token)
            conn.execute(
                f"""
                UPDATE course_processing_jobs
                SET status = ?, stage = ?, failed_stage = ?, error_code = ?,
                    lease_token = NULL, lease_until = NULL, updated_at = ?
                {where}
                """,
                parameters,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM course_processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed processing job disappeared")
            return self._job_from_row(row)

    def get_processing_job(self, course_id: str) -> CourseProcessingJob | None:
        canonical = _validate_course_id(course_id)
        self.initialize()
        with self._connect() as conn:
            return self._job_with_connection(conn, course_id=canonical)

    def get_processing_job_by_id(self, job_id: str) -> CourseProcessingJob | None:
        canonical = _validate_uuid(job_id, "job id")
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT j.* FROM course_processing_jobs AS j
                JOIN courses AS c ON c.id = j.course_id
                WHERE j.id = ? AND c.owner_scope = ?
                """,
                (canonical, self.owner_scope),
            ).fetchone()
            return self._job_from_row(row) if row is not None else None

    def list_sources(self, course_id: str) -> tuple[CourseSource, ...]:
        canonical = _validate_course_id(course_id)
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM course_sources AS s
                JOIN courses AS c ON c.id = s.course_id
                WHERE s.course_id = ? AND c.owner_scope = ?
                ORDER BY s.created_at, s.id
                """,
                (canonical, self.owner_scope),
            ).fetchall()
            return tuple(self._source_from_row(row) for row in rows)

    def get_manifest(self, course_id: str) -> CourseManifest:
        canonical = _validate_course_id(course_id)
        self.initialize()
        with self._connect() as conn:
            job = self._job_with_connection(conn, course_id=canonical)
            if job is None:
                raise InvalidCourseIdentifierError("Course manifest not found")
            entries = self._manifest_entries_with_connection(conn, canonical)
            blockers = _manifest_blockers(entries)
            return CourseManifest(
                course_id=canonical,
                revision=job.manifest_revision,
                entries=entries,
                blockers=blockers,
                eligible_for_planning=(job.status is CourseJobStatus.COMPLETED and not blockers),
            )

    def update_manifest(
        self,
        course_id: str,
        expected_revision: int,
        updates: Iterable[dict[str, object]],
    ) -> CourseManifest:
        canonical = _validate_course_id(course_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ManifestRevisionConflictError("Manifest revision is invalid")
        update_items = tuple(updates)
        self.initialize()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._job_with_connection(conn, course_id=canonical)
            if job is None:
                raise InvalidCourseIdentifierError("Course manifest not found")
            if job.status is not CourseJobStatus.AWAITING_MANIFEST_REVIEW:
                raise InvalidManifestStateError("Manifest review is not active")
            if job.manifest_revision != expected_revision:
                raise ManifestRevisionConflictError("Manifest revision is stale")
            current = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT * FROM course_manifest_entries WHERE course_id = ?",
                    (canonical,),
                ).fetchall()
            }
            if not update_items:
                raise InvalidCourseInputError("Manifest update must include entries")
            source_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM course_sources WHERE course_id = ?",
                    (canonical,),
                ).fetchone()[0]
            )
            if not current or len(current) != source_count:
                raise InvalidManifestError(
                    "Manifest must contain exactly one entry per accepted source"
                )
            if len(update_items) != source_count or {
                item.get("id") if isinstance(item, dict) else None for item in update_items
            } != set(current):
                raise InvalidManifestError(
                    "Manifest updates must include exactly one entry per accepted source"
                )
            now = datetime.now(timezone.utc).isoformat()
            seen_update_ids: set[str] = set()
            for update in update_items:
                entry_id = update.get("id") if isinstance(update, dict) else None
                if not isinstance(entry_id, str) or entry_id not in current:
                    raise InvalidCourseInputError("Manifest entry is invalid")
                if entry_id in seen_update_ids:
                    raise InvalidCourseInputError("Manifest entry is duplicated")
                seen_update_ids.add(entry_id)
                row = current[entry_id]
                has_role = "role" in update
                has_visibility = "visibility" in update
                try:
                    role = ManifestRole(str(update.get("role", row["role"])))
                    visibility = ManifestVisibility(
                        str(update.get("visibility", row["visibility"]))
                    )
                except ValueError as exc:
                    raise InvalidCourseInputError("Manifest role or visibility is invalid") from exc
                for confirmation_key in ("role_confirmed", "visibility_confirmed"):
                    if confirmation_key in update and not isinstance(
                        update[confirmation_key], bool
                    ):
                        raise InvalidCourseInputError("Manifest confirmation is invalid")
                role_confirmed = (
                    bool(update["role_confirmed"])
                    if "role_confirmed" in update
                    else bool(row["role_confirmed"]) or (has_role and role.value != row["role"])
                )
                visibility_confirmed = (
                    bool(update["visibility_confirmed"])
                    if "visibility_confirmed" in update
                    else bool(row["visibility_confirmed"])
                    or (has_visibility and visibility.value != row["visibility"])
                )
                suspected_solution = bool(row["suspected_solution"])
                conn.execute(
                    """
                    UPDATE course_manifest_entries
                    SET role = ?, visibility = ?, suspected_solution = ?,
                        role_confirmed = ?, visibility_confirmed = ?, updated_at = ?
                    WHERE id = ? AND course_id = ?
                    """,
                    (
                        role.value,
                        visibility.value,
                        int(suspected_solution),
                        int(role_confirmed),
                        int(visibility_confirmed),
                        now,
                        entry_id,
                        canonical,
                    ),
                )
            next_revision = expected_revision + 1
            conn.execute(
                """
                UPDATE course_processing_jobs SET manifest_revision = ?, updated_at = ?
                WHERE course_id = ?
                """,
                (next_revision, now, canonical),
            )
            conn.execute(
                """
                INSERT INTO course_audit_events(
                    course_id, event_type, from_status, to_status, payload_json, occurred_at
                ) VALUES (?, 'course.manifest_updated', ?, ?, ?, ?)
                """,
                (
                    canonical,
                    CourseStatus.DRAFT.value,
                    CourseStatus.DRAFT.value,
                    json.dumps(
                        {"manifest_revision": next_revision, "entry_count": len(update_items)},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            conn.commit()
            return self.get_manifest(canonical)

    def approve_manifest(self, course_id: str, expected_revision: int) -> CourseManifest:
        canonical = _validate_course_id(course_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ManifestRevisionConflictError("Manifest revision is invalid")
        self.initialize()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._job_with_connection(conn, course_id=canonical)
            if job is None:
                raise InvalidCourseIdentifierError("Course manifest not found")
            if job.status is not CourseJobStatus.AWAITING_MANIFEST_REVIEW:
                raise InvalidManifestStateError("Manifest approval is not active")
            if job.manifest_revision != expected_revision:
                raise ManifestRevisionConflictError("Manifest revision is stale")
            entries = self._manifest_entries_with_connection(conn, canonical)
            source_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM course_sources WHERE course_id = ?",
                    (canonical,),
                ).fetchone()[0]
            )
            if not entries or len(entries) != source_count:
                raise InvalidManifestError(
                    "Manifest must contain exactly one entry per accepted source"
                )
            blockers = _manifest_blockers(entries)
            if blockers:
                raise ManifestApprovalBlockedError(blockers)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE course_processing_jobs
                SET status = ?, stage = ?, failed_stage = NULL, error_code = NULL, updated_at = ?
                WHERE course_id = ?
                """,
                (
                    CourseJobStatus.COMPLETED.value,
                    CourseJobStage.COMPLETED.value,
                    now,
                    canonical,
                ),
            )
            conn.execute(
                """
                INSERT INTO course_audit_events(
                    course_id, event_type, from_status, to_status, payload_json, occurred_at
                ) VALUES (?, 'course.manifest_approved', ?, ?, ?, ?)
                """,
                (
                    canonical,
                    CourseStatus.DRAFT.value,
                    CourseStatus.DRAFT.value,
                    json.dumps(
                        {
                            "manifest_revision": expected_revision,
                            "planning_eligible": True,
                            "planning_owner": "OWE-8/OWE-9",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            conn.commit()
        return self.get_manifest(canonical)

    def list(
        self,
        *,
        limit: int = DEFAULT_COURSE_LIST_LIMIT,
        offset: int = 0,
    ) -> list[Course]:
        return list(self.list_page(limit=limit, offset=offset).courses)

    def list_page(
        self,
        *,
        limit: int = DEFAULT_COURSE_LIST_LIMIT,
        offset: int = 0,
    ) -> CoursePage:
        if not 1 <= limit <= MAX_COURSE_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_COURSE_LIST_LIMIT}")
        if not 0 <= offset <= MAX_COURSE_LIST_OFFSET:
            raise ValueError(f"offset must be between 0 and {MAX_COURSE_LIST_OFFSET}")
        self.initialize()
        with self._connect() as conn:
            conn.execute("BEGIN")
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM courses WHERE owner_scope = ?",
                    (self.owner_scope,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT
                    c.id, c.title, c.description, c.status, c.workspace_ref,
                    c.created_at, c.updated_at, c.desired_outcome, c.weekly_minutes,
                    c.ocw_url, c.scheduling_json, c.difficulty, c.accessibility,
                    u.id AS unit_id, u.title AS unit_title, u.position AS unit_position
                    ,j.id AS processing_job_id
                FROM courses AS c
                LEFT JOIN units AS u ON u.course_id = c.id
                LEFT JOIN course_processing_jobs AS j ON j.course_id = c.id
                WHERE c.owner_scope = ?
                ORDER BY c.updated_at DESC, c.id DESC
                LIMIT ? OFFSET ?
                """,
                (self.owner_scope, limit, offset),
            ).fetchall()
            courses = tuple(self._course_from_list_row(row) for row in rows)
            conn.commit()
            return CoursePage(
                courses=courses,
                total=total,
                limit=limit,
                offset=offset,
            )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> CourseProcessingJob:
        return CourseProcessingJob(
            id=row["id"],
            course_id=row["course_id"],
            status=CourseJobStatus(row["status"]),
            stage=CourseJobStage(row["stage"]),
            failed_stage=(CourseJobStage(row["failed_stage"]) if row["failed_stage"] else None),
            error_code=row["error_code"],
            attempt_count=row["attempt_count"],
            manifest_revision=row["manifest_revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _job_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        course_id: str,
    ) -> CourseProcessingJob | None:
        row = conn.execute(
            """
            SELECT j.* FROM course_processing_jobs AS j
            JOIN courses AS c ON c.id = j.course_id
            WHERE j.course_id = ? AND c.owner_scope = ?
            """,
            (course_id, self.owner_scope),
        ).fetchone()
        return self._job_from_row(row) if row is not None else None

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> CourseSource:
        return CourseSource(
            id=row["id"],
            course_id=row["course_id"],
            unit_id=row["unit_id"],
            original_filename=row["original_filename"],
            display_filename=row["display_filename"],
            relative_path=row["relative_path"],
            content_hash=row["content_hash"],
            size_bytes=row["size_bytes"],
            extracted_chars=row["extracted_chars"],
            created_at=row["created_at"],
        )

    def _manifest_entries_with_connection(
        self,
        conn: sqlite3.Connection,
        course_id: str,
    ) -> tuple[ManifestEntry, ...]:
        rows = conn.execute(
            """
            SELECT m.*, s.original_filename, s.display_filename
            FROM course_manifest_entries AS m
            JOIN course_sources AS s ON s.id = m.source_id
            WHERE m.course_id = ?
            ORDER BY m.created_at, m.id
            """,
            (course_id,),
        ).fetchall()
        return tuple(
            ManifestEntry(
                id=row["id"],
                course_id=row["course_id"],
                source_id=row["source_id"],
                original_filename=row["original_filename"],
                display_filename=row["display_filename"],
                role=ManifestRole(row["role"]),
                visibility=ManifestVisibility(row["visibility"]),
                suspected_solution=bool(row["suspected_solution"]),
                role_confirmed=bool(row["role_confirmed"]),
                visibility_confirmed=bool(row["visibility_confirmed"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    @staticmethod
    def _course_from_list_row(row: sqlite3.Row) -> Course:
        units: tuple[Unit, ...] = ()
        if row["unit_id"] is not None:
            units = (
                Unit(
                    id=row["unit_id"],
                    course_id=row["id"],
                    title=row["unit_title"],
                    position=row["unit_position"],
                ),
            )
        return Course(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=CourseStatus(row["status"]),
            workspace_ref=row["workspace_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            units=units,
            desired_outcome=row["desired_outcome"],
            weekly_minutes=row["weekly_minutes"],
            ocw_url=row["ocw_url"],
            scheduling=row["scheduling_json"],
            difficulty=row["difficulty"],
            accessibility=row["accessibility"],
            processing_job_id=row["processing_job_id"],
        )

    def get(self, course_id: str) -> Course | None:
        canonical = _validate_course_id(course_id)
        self.initialize()
        with self._connect() as conn:
            return self._get_with_connection(conn, canonical)

    def _get_with_connection(self, conn: sqlite3.Connection, course_id: str) -> Course | None:
        row = conn.execute(
            """
            SELECT c.*, j.id AS processing_job_id
            FROM courses AS c
            LEFT JOIN course_processing_jobs AS j ON j.course_id = c.id
            WHERE c.id = ? AND c.owner_scope = ?
            """,
            (course_id, self.owner_scope),
        ).fetchone()
        if row is None:
            return None
        unit_rows = conn.execute(
            "SELECT id, course_id, title, position FROM units WHERE course_id = ? ORDER BY position",
            (course_id,),
        ).fetchall()
        return Course(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=CourseStatus(row["status"]),
            workspace_ref=row["workspace_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            units=tuple(Unit(**dict(unit_row)) for unit_row in unit_rows),
            desired_outcome=row["desired_outcome"],
            weekly_minutes=row["weekly_minutes"],
            ocw_url=row["ocw_url"],
            scheduling=row["scheduling_json"],
            difficulty=row["difficulty"],
            accessibility=row["accessibility"],
            processing_job_id=row["processing_job_id"],
        )


__all__ = [
    "DEFAULT_COURSE_LIST_LIMIT",
    "DEFAULT_MAX_COURSES_PER_OWNER",
    "MAX_COURSE_LIST_LIMIT",
    "MAX_COURSE_LIST_OFFSET",
    "CourseInput",
    "CourseImportResult",
    "CoursePage",
    "CourseQuotaExceededError",
    "CourseRepository",
    "CreateCourseResult",
    "IdempotencyConflictError",
    "InvalidManifestError",
    "InvalidManifestStateError",
    "InvalidJobRetryError",
    "InvalidCourseIdentifierError",
    "InvalidCourseInputError",
    "InvalidRequestKeyError",
    "ManifestApprovalBlockedError",
    "ManifestRevisionConflictError",
    "UploadIdentity",
    "UnsupportedSchemaVersionError",
]
