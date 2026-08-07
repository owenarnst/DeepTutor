from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from deeptutor.course_mode.models import CourseStatus
from deeptutor.course_mode.repository import (
    DEFAULT_COURSE_LIST_LIMIT,
    MAX_COURSE_LIST_LIMIT,
    CourseInput,
    CourseQuotaExceededError,
    CourseRepository,
    IdempotencyConflictError,
    InvalidCourseIdentifierError,
)
from deeptutor.services.path_service import PathService


@pytest.fixture
def paths(tmp_path: Path) -> PathService:
    return PathService(workspace_root=tmp_path / "data")


@pytest.fixture
def repository(paths: PathService) -> CourseRepository:
    return CourseRepository(paths, owner_scope="user-a")


def test_create_persists_one_unit_workspace_and_audit_across_restart(
    repository: CourseRepository, paths: PathService
) -> None:
    result = repository.create_draft(
        request_key="create-course-001",
        course_input=CourseInput(
            title="  Linear   Algebra  ",
            description="  Vectors and matrices  ",
            unit_title="  Foundations  ",
        ),
    )

    assert result.created is True
    assert result.course.title == "Linear Algebra"
    assert result.course.description == "Vectors and matrices"
    assert result.course.status is CourseStatus.DRAFT
    assert len(result.course.units) == 1
    assert result.course.units[0].course_id == result.course.id
    assert result.course.units[0].title == "Foundations"
    assert result.course.workspace_ref == f"courses/{result.course.id}"
    assert paths.get_course_mode_db() != paths.get_chat_history_db()
    assert paths.get_course_workspace(result.course.id).is_dir()

    reopened = CourseRepository(paths, owner_scope="user-a").get(result.course.id)
    assert reopened == result.course

    with sqlite3.connect(paths.get_course_mode_db()) as conn:
        conn.row_factory = sqlite3.Row
        audit = conn.execute(
            "SELECT event_type, from_status, to_status FROM course_audit_events"
        ).fetchall()
        assert [dict(row) for row in audit] == [
            {
                "event_type": "course.created",
                "from_status": None,
                "to_status": "draft",
            }
        ]


def test_create_replays_normalized_input_and_rejects_changed_input(
    repository: CourseRepository,
) -> None:
    first = repository.create_draft(
        "stable-key",
        CourseInput(title="Graph Theory", description=" Paths ", unit_title="Unit 1"),
    )
    replay = repository.create_draft(
        "stable-key",
        CourseInput(title=" Graph   Theory ", description="Paths", unit_title="Unit 1"),
    )

    assert replay.created is False
    assert replay.course == first.course
    assert len(repository.list()) == 1

    with pytest.raises(IdempotencyConflictError):
        repository.create_draft(
            "stable-key",
            CourseInput(title="Different course", description="Paths", unit_title="Unit 1"),
        )


def test_concurrent_retries_create_one_course(repository: CourseRepository) -> None:
    def create_once(_: int) -> str:
        repo = CourseRepository(repository.path_service, owner_scope="user-a")
        return repo.create_draft(
            "concurrent-key",
            CourseInput(title="Probability", unit_title="Random variables"),
        ).course.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create_once, range(16)))

    assert len(set(ids)) == 1
    assert len(repository.list()) == 1

    with sqlite3.connect(repository.path_service.get_course_mode_db()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM course_audit_events").fetchone()[0] == 1


def test_course_unit_and_audit_are_atomic(repository: CourseRepository) -> None:
    # A database-level failure on the final audit write must roll back the
    # earlier aggregate rows in the same transaction.
    repository.initialize()
    with sqlite3.connect(repository.path_service.get_course_mode_db()) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_audit BEFORE INSERT ON course_audit_events
            BEGIN SELECT RAISE(ABORT, 'audit disabled'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_draft("atomic-key", CourseInput(title="Calculus"))

    with sqlite3.connect(repository.path_service.get_course_mode_db()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0


def test_audit_events_are_append_only(repository: CourseRepository) -> None:
    repository.create_draft("audit-key", CourseInput(title="Calculus"))

    with sqlite3.connect(repository.path_service.get_course_mode_db()) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE course_audit_events SET to_status = 'changed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM course_audit_events")


def test_initializes_an_existing_empty_database(paths: PathService) -> None:
    db_path = paths.get_course_mode_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()

    CourseRepository(paths, owner_scope="user-a").initialize()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"courses", "units", "course_audit_events", "artifact_references"} <= tables


def test_migrates_an_older_schema_without_losing_courses(paths: PathService) -> None:
    db_path = paths.get_course_mode_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE courses (
                id TEXT PRIMARY KEY,
                owner_scope TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                workspace_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE units (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL UNIQUE REFERENCES courses(id),
                title TEXT NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE TABLE idempotency_keys (
                owner_scope TEXT NOT NULL,
                request_key TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                course_id TEXT NOT NULL REFERENCES courses(id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_scope, request_key)
            );
            CREATE TABLE course_audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id TEXT NOT NULL REFERENCES courses(id),
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            INSERT INTO courses VALUES (
                '11111111-1111-4111-8111-111111111111', 'user-a', 'Existing', '',
                'draft', 'courses/11111111-1111-4111-8111-111111111111',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO units VALUES (
                '22222222-2222-4222-8222-222222222222',
                '11111111-1111-4111-8111-111111111111', 'Unit 1', 0
            );
            PRAGMA user_version = 1;
            """
        )

    repository = CourseRepository(paths, owner_scope="user-a")
    existing = repository.get("11111111-1111-4111-8111-111111111111")
    assert existing is not None
    assert existing.title == "Existing"

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {row[1] for row in conn.execute("PRAGMA table_info(artifact_references)")}
        assert "relative_path" in columns
        assert "body" not in columns
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert {
            "course_audit_events_no_update",
            "course_audit_events_no_delete",
        } <= triggers


def test_invalid_course_ids_never_reach_the_filesystem(
    repository: CourseRepository, paths: PathService
) -> None:
    for invalid in ("../foreign", "not-a-uuid", "", "a/b", "a\\b"):
        with pytest.raises(InvalidCourseIdentifierError):
            repository.get(invalid)
        with pytest.raises(ValueError):
            paths.get_course_workspace(invalid)


def test_separate_path_services_are_isolated(tmp_path: Path) -> None:
    alice_paths = PathService(workspace_root=tmp_path / "alice")
    bob_paths = PathService(workspace_root=tmp_path / "bob")
    alice = CourseRepository(alice_paths, owner_scope="alice")
    bob = CourseRepository(bob_paths, owner_scope="bob")

    created = alice.create_draft("same-key", CourseInput(title="Alice course")).course
    bob_course = bob.create_draft("same-key", CourseInput(title="Bob course")).course

    assert bob.get(created.id) is None
    assert alice.get(bob_course.id) is None
    assert [course.title for course in alice.list()] == ["Alice course"]
    assert [course.title for course in bob.list()] == ["Bob course"]


def test_course_quota_is_transactional_and_replay_succeeds_at_limit(
    paths: PathService,
) -> None:
    repository = CourseRepository(paths, owner_scope="user-a", max_courses=2)
    first = repository.create_draft("course-1", CourseInput(title="One"))
    repository.create_draft("course-2", CourseInput(title="Two"))

    with pytest.raises(CourseQuotaExceededError, match="2"):
        repository.create_draft("course-3", CourseInput(title="Three"))

    replay = repository.create_draft("course-1", CourseInput(title="One"))
    assert replay.created is False
    assert replay.course.id == first.course.id


def test_list_is_bounded_and_validates_pagination(repository: CourseRepository) -> None:
    for index in range(5):
        repository.create_draft(f"course-{index}", CourseInput(title=f"Course {index}"))

    assert len(repository.list(limit=2)) == 2
    assert len(repository.list(limit=2, offset=2)) == 2
    assert len(repository.list()) <= DEFAULT_COURSE_LIST_LIMIT
    with pytest.raises(ValueError):
        repository.list(limit=MAX_COURSE_LIST_LIMIT + 1)
    with pytest.raises(ValueError):
        repository.list(offset=-1)


def test_current_schema_reads_do_not_open_write_transactions_or_run_n_plus_one_queries(
    repository: CourseRepository, monkeypatch
) -> None:
    repository.create_draft("course-1", CourseInput(title="One"))
    repository.create_draft("course-2", CourseInput(title="Two"))
    statements: list[str] = []
    original_connect = repository._connect

    @contextmanager
    def traced_connect():
        with original_connect() as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(repository, "_connect", traced_connect)
    assert len(repository.list()) == 2

    normalized = [statement.strip().upper() for statement in statements]
    assert not any(statement.startswith("BEGIN IMMEDIATE") for statement in normalized)
    data_selects = [
        statement
        for statement in normalized
        if statement.startswith("SELECT") and " FROM COURSES" in statement
    ]
    assert len(data_selects) == 1
