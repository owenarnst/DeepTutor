from __future__ import annotations

import os
from pathlib import Path
import sqlite3

import pytest

from deeptutor.course_mode.artifacts import (
    InvalidArtifactPathError,
    UnsupportedCourseStorageError,
    ensure_course_workspace,
    normalize_artifact_relative_path,
    open_course_artifact_for_read,
    remove_empty_course_workspace,
    write_course_artifact_atomic,
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
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


@pytest.mark.parametrize(
    "boundary",
    ["anchor", "system", "course-mode", "scopes", "scope", "workspace", "courses", "course"],
)
def test_course_workspace_creation_rejects_symlinked_storage_ancestors(
    paths: PathService,
    tmp_path: Path,
    boundary: str,
) -> None:
    course_id = "11111111-1111-4111-8111-111111111111"
    storage_root = paths.get_course_storage_root()
    storage_anchor = paths.get_course_storage_anchor()
    targets = {
        "anchor": storage_anchor,
        "system": storage_anchor / "system",
        "course-mode": storage_anchor / "system" / "course-mode",
        "scopes": storage_anchor / "system" / "course-mode" / "scopes",
        "scope": storage_root,
        "workspace": paths.get_course_mode_workspace_root(),
        "courses": paths.get_course_mode_workspace_root() / "courses",
        "course": paths.get_course_workspace(course_id),
    }
    target = targets[boundary]
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / f"outside-{boundary}"
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidArtifactPathError, match="symlink|reparse"):
        ensure_course_workspace(paths, course_id)


def test_artifact_read_rejects_symlinked_descendant(
    paths: PathService,
    tmp_path: Path,
) -> None:
    course_id = "11111111-1111-4111-8111-111111111111"
    ensure_course_workspace(paths, course_id)
    outside = tmp_path / "outside-descendant"
    outside.mkdir()
    (outside / "lesson.md").write_text("outside", encoding="utf-8")
    (paths.get_course_workspace(course_id) / "notes").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidArtifactPathError, match="symlink|reparse"):
        with open_course_artifact_for_read(paths, course_id, "notes/lesson.md"):
            pass


def test_artifact_write_rejects_symlinked_descendant(
    paths: PathService,
    tmp_path: Path,
) -> None:
    course_id = "11111111-1111-4111-8111-111111111111"
    ensure_course_workspace(paths, course_id)
    outside = tmp_path / "outside-write"
    outside.mkdir()
    (paths.get_course_workspace(course_id) / "sources").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(InvalidArtifactPathError, match="symlink|reparse"):
        write_course_artifact_atomic(paths, course_id, "sources/lesson.md", b"inside")
    assert not (outside / "lesson.md").exists()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="directory-relative no-follow descriptors are unavailable",
)
def test_artifact_open_is_safe_when_ancestor_is_replaced_during_operation(
    paths: PathService,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from deeptutor.course_mode import artifacts

    course_id = "11111111-1111-4111-8111-111111111111"
    ensure_course_workspace(paths, course_id)
    notes = paths.get_course_workspace(course_id) / "notes"
    notes.mkdir()
    (notes / "lesson.md").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside-race"
    outside.mkdir()
    (outside / "lesson.md").write_text("outside", encoding="utf-8")
    detached = notes.with_name("notes-detached")
    real_open = artifacts.os.open
    swapped = False

    def swap_before_leaf(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "lesson.md" and not swapped:
            notes.rename(detached)
            notes.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts.os, "open", swap_before_leaf)

    with open_course_artifact_for_read(paths, course_id, "notes/lesson.md") as artifact:
        assert artifact.read() == b"inside"
    assert swapped is True


def test_workspace_creation_fails_closed_without_handle_relative_traversal(
    paths: PathService,
    monkeypatch,
) -> None:
    from deeptutor.course_mode import artifacts

    monkeypatch.setattr(artifacts, "_SECURE_DIR_FD_SUPPORTED", False)

    with pytest.raises(UnsupportedCourseStorageError, match="handle-relative"):
        ensure_course_workspace(paths, "11111111-1111-4111-8111-111111111111")


def test_artifact_read_fails_closed_without_handle_relative_traversal(
    paths: PathService,
    monkeypatch,
) -> None:
    from deeptutor.course_mode import artifacts

    course_id = "11111111-1111-4111-8111-111111111111"
    ensure_course_workspace(paths, course_id)
    artifact_path = paths.get_course_workspace(course_id) / "lesson.md"
    artifact_path.write_text("inside", encoding="utf-8")
    monkeypatch.setattr(artifacts, "_SECURE_DIR_FD_SUPPORTED", False)

    with pytest.raises(UnsupportedCourseStorageError, match="handle-relative"):
        with open_course_artifact_for_read(paths, course_id, "lesson.md"):
            pass


def test_workspace_rollback_fails_closed_without_handle_relative_traversal(
    paths: PathService,
    monkeypatch,
) -> None:
    from deeptutor.course_mode import artifacts

    course_id = "11111111-1111-4111-8111-111111111111"
    ensure_course_workspace(paths, course_id)
    workspace = paths.get_course_workspace(course_id)
    monkeypatch.setattr(artifacts, "_SECURE_DIR_FD_SUPPORTED", False)

    with pytest.raises(UnsupportedCourseStorageError, match="handle-relative"):
        remove_empty_course_workspace(paths, course_id)
    assert workspace.is_dir()
