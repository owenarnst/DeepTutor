from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from deeptutor.course_mode.artifacts import (
    InvalidArtifactPathError,
    normalize_artifact_relative_path,
    resolve_course_artifact_path,
)
from deeptutor.course_mode.repository import CourseInput, CourseRepository
from deeptutor.services.path_service import PathService


@pytest.fixture
def paths(tmp_path: Path) -> PathService:
    return PathService(workspace_root=tmp_path / "data")


@pytest.fixture
def repository(paths: PathService) -> CourseRepository:
    return CourseRepository(paths, owner_scope="user-a")


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        ".",
        "..",
        "./lesson.md",
        "notes/../lesson.md",
        "notes/./lesson.md",
        "/absolute.md",
        "//server/share.md",
        r"\\server\share.md",
        r"notes\lesson.md",
        "C:lesson.md",
        "C:/lesson.md",
        "notes//lesson.md",
        "notes/",
        "notes/lesson\x00.md",
    ],
)
def test_artifact_relative_path_rejects_nonportable_or_unsafe_values(
    relative_path: str,
) -> None:
    with pytest.raises(InvalidArtifactPathError):
        normalize_artifact_relative_path(relative_path)


def test_artifact_relative_path_preserves_canonical_posix_storage() -> None:
    assert normalize_artifact_relative_path("unit-1/notes/lesson.md") == ("unit-1/notes/lesson.md")


def test_artifact_database_rejects_a_unit_owned_by_another_course(
    repository: CourseRepository,
) -> None:
    course_a = repository.create_draft("course-a", CourseInput(title="A")).course
    course_b = repository.create_draft("course-b", CourseInput(title="B")).course

    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO artifact_references(
                    id, course_id, unit_id, kind, relative_path, content_hash, created_at
                ) VALUES (?, ?, ?, 'note', 'notes/a.md', 'sha256:test', '2026-01-01')
                """,
                ("artifact-a", course_a.id, course_b.units[0].id),
            )


def test_v2_artifact_schema_migrates_valid_rows_and_adds_composite_fk(
    repository: CourseRepository,
) -> None:
    course = repository.create_draft("course-a", CourseInput(title="A")).course
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE artifact_references")
        conn.execute(
            """
            CREATE TABLE artifact_references (
                id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL REFERENCES courses(id),
                unit_id TEXT REFERENCES units(id),
                kind TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(course_id, relative_path),
                CHECK(substr(relative_path, 1, 1) != '/'),
                CHECK(instr(relative_path, '..') = 0)
            )
            """
        )
        conn.execute(
            "INSERT INTO artifact_references VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "artifact-a",
                course.id,
                course.units[0].id,
                "note",
                "notes/a.md",
                "sha256:test",
                "2026-01-01",
            ),
        )
        conn.execute("PRAGMA user_version = 2")

    repository.initialize()

    with sqlite3.connect(repository.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute(
            "SELECT relative_path FROM artifact_references WHERE id = 'artifact-a'"
        ).fetchone() == ("notes/a.md",)
        foreign_keys = conn.execute("PRAGMA foreign_key_list(artifact_references)").fetchall()
        assert any(row[2] == "units" and row[3] == "course_id" for row in foreign_keys)
        assert any(row[2] == "units" and row[3] == "unit_id" for row in foreign_keys)


def test_v2_migration_rejects_cross_course_artifact_rows(
    repository: CourseRepository,
) -> None:
    course_a = repository.create_draft("course-a", CourseInput(title="A")).course
    course_b = repository.create_draft("course-b", CourseInput(title="B")).course
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE artifact_references SET unit_id = NULL")
        conn.execute(
            "INSERT INTO artifact_references VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "artifact-invalid",
                course_a.id,
                course_b.units[0].id,
                "note",
                "notes/invalid.md",
                "sha256:test",
                "2026-01-01",
            ),
        )
        conn.execute("PRAGMA user_version = 2")

    with pytest.raises(sqlite3.IntegrityError):
        repository.initialize()


def test_artifact_resolution_is_contained_and_rejects_symlink_components(
    repository: CourseRepository, paths: PathService, tmp_path: Path
) -> None:
    course = repository.create_draft("course-a", CourseInput(title="A")).course
    resolved = resolve_course_artifact_path(paths, course.id, "notes/lesson.md")
    assert resolved == paths.get_course_workspace(course.id) / "notes" / "lesson.md"

    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = paths.get_course_workspace(course.id) / "linked"
    symlink.symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidArtifactPathError, match="symlink"):
        resolve_course_artifact_path(paths, course.id, "linked/escape.md")
