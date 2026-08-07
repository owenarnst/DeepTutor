"""Transactional SQLite repository for the Course aggregate."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from uuid import UUID, uuid4

from deeptutor.services.path_service import PathService

from .models import Course, CourseStatus, Unit

_LATEST_SCHEMA_VERSION = 2
_REQUEST_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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


class CourseRepository:
    """One repository instance serves exactly one server-derived user scope."""

    def __init__(self, path_service: PathService, *, owner_scope: str):
        if not owner_scope:
            raise ValueError("owner_scope is required")
        self.path_service = path_service
        self.owner_scope = owner_scope
        self.db_path = path_service.get_course_mode_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
            conn.execute("BEGIN IMMEDIATE")
            try:
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
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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

    def create_draft(self, request_key: str, course_input: CourseInput) -> CreateCourseResult:
        if not _REQUEST_KEY_RE.fullmatch(request_key):
            raise InvalidRequestKeyError("Idempotency key must be 1-128 URL-safe characters")
        normalized = _normalize_input(course_input)
        request_fingerprint = _fingerprint(normalized)
        self.initialize()

        workspace: Path | None = None
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

                course_id = str(uuid4())
                unit_id = str(uuid4())
                now = datetime.now(timezone.utc).isoformat()
                workspace_ref = f"courses/{course_id}"
                conn.execute(
                    """
                    INSERT INTO courses(
                        id, owner_scope, title, description, status, workspace_ref,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
                conn.execute(
                    "INSERT INTO units(id, course_id, title, position) VALUES (?, ?, ?, 0)",
                    (unit_id, course_id, normalized.unit_title),
                )
                conn.execute(
                    """
                    INSERT INTO idempotency_keys(
                        owner_scope, request_key, request_fingerprint, course_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.owner_scope, request_key, request_fingerprint, course_id, now),
                )

                workspace = self.path_service.get_course_workspace(course_id)
                workspace_created = not workspace.exists()
                workspace.mkdir(parents=True, exist_ok=True)

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
                if workspace_created and workspace is not None:
                    try:
                        workspace.rmdir()
                    except OSError:
                        pass
                raise

    def list(self) -> list[Course]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM courses
                WHERE owner_scope = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (self.owner_scope,),
            ).fetchall()
            return [
                course for row in rows if (course := self._get_with_connection(conn, row["id"]))
            ]

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
    "CourseInput",
    "CourseRepository",
    "CreateCourseResult",
    "IdempotencyConflictError",
    "InvalidCourseIdentifierError",
    "InvalidCourseInputError",
    "InvalidRequestKeyError",
    "UnsupportedSchemaVersionError",
]
