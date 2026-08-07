"""Transactional SQLite repository for the Course aggregate."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Iterator
from uuid import UUID, uuid4

from deeptutor.services.path_service import PathService

from .artifacts import (
    InvalidArtifactPathError,
    ensure_course_data_root,
    ensure_course_workspace,
    remove_empty_course_workspace,
)
from .models import Course, CourseStatus, Unit

_LATEST_SCHEMA_VERSION = 4
_REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEFAULT_MAX_COURSES_PER_OWNER = 200
DEFAULT_COURSE_LIST_LIMIT = 50
MAX_COURSE_LIST_LIMIT = 100
MAX_COURSE_LIST_OFFSET = 10_000


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


@dataclass(frozen=True)
class CourseInput:
    title: str
    description: str = ""
    unit_title: str = "Unit 1"


@dataclass(frozen=True)
class CreateCourseResult:
    course: Course
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


def _normalize_input(value: CourseInput) -> CourseInput:
    normalized = CourseInput(
        title=_normalize_text(value.title),
        description=_normalize_text(value.description),
        unit_title=_normalize_text(value.unit_title),
    )
    if not normalized.title or len(normalized.title) > 200:
        raise InvalidCourseInputError("Course title must be between 1 and 200 characters")
    if len(normalized.description) > 2000:
        raise InvalidCourseInputError("Course description must be at most 2000 characters")
    if not normalized.unit_title or len(normalized.unit_title) > 200:
        raise InvalidCourseInputError("Unit title must be between 1 and 200 characters")
    return normalized


def _fingerprint(value: CourseInput) -> str:
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


def _validate_course_id(course_id: str) -> str:
    try:
        parsed = UUID(course_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidCourseIdentifierError("Invalid course id") from exc
    canonical = str(parsed)
    if course_id != canonical:
        raise InvalidCourseIdentifierError("Invalid course id")
    return canonical


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
    ):
        if not owner_scope:
            raise ValueError("owner_scope is required")
        if not 1 <= max_courses <= 10_000:
            raise ValueError("max_courses must be between 1 and 10000")
        self.path_service = path_service
        self.owner_scope = owner_scope
        self.max_courses = max_courses
        self.db_path = path_service.get_course_mode_db()

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

    def create_draft(self, request_key: str, course_input: CourseInput) -> CreateCourseResult:
        if not _REQUEST_KEY_RE.fullmatch(request_key):
            raise InvalidRequestKeyError("Idempotency key must be 1-128 URL-safe characters")
        normalized = _normalize_input(course_input)
        request_fingerprint = _fingerprint(normalized)
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
                        created_at, updated_at, primary_unit_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    c.created_at, c.updated_at,
                    u.id AS unit_id, u.title AS unit_title, u.position AS unit_position
                FROM courses AS c
                LEFT JOIN units AS u ON u.course_id = c.id
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
        )

    def get(self, course_id: str) -> Course | None:
        canonical = _validate_course_id(course_id)
        self.initialize()
        with self._connect() as conn:
            return self._get_with_connection(conn, canonical)

    def _get_with_connection(self, conn: sqlite3.Connection, course_id: str) -> Course | None:
        row = conn.execute(
            "SELECT * FROM courses WHERE id = ? AND owner_scope = ?",
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
        )


__all__ = [
    "DEFAULT_COURSE_LIST_LIMIT",
    "DEFAULT_MAX_COURSES_PER_OWNER",
    "MAX_COURSE_LIST_LIMIT",
    "MAX_COURSE_LIST_OFFSET",
    "CourseInput",
    "CoursePage",
    "CourseQuotaExceededError",
    "CourseRepository",
    "CreateCourseResult",
    "IdempotencyConflictError",
    "InvalidCourseIdentifierError",
    "InvalidCourseInputError",
    "InvalidRequestKeyError",
    "UnsupportedSchemaVersionError",
]
