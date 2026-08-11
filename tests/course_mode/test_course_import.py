from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from deeptutor.course_mode.models import CourseJobStatus, ManifestRole, ManifestVisibility
from deeptutor.course_mode.repository import (
    CourseInput,
    CourseRepository,
    IdempotencyConflictError,
    InvalidJobRetryError,
    ManifestApprovalBlockedError,
    ManifestRevisionConflictError,
)
from deeptutor.services.path_service import PathService


@pytest.fixture
def repository(tmp_path: Path) -> CourseRepository:
    return CourseRepository(PathService(workspace_root=tmp_path / "data"), owner_scope="user-a")


def _input() -> CourseInput:
    return CourseInput(
        title="Linear algebra",
        unit_title="Foundations",
        desired_outcome="Solve systems of linear equations",
        weekly_minutes=120,
        ocw_url="https://ocw.mit.edu/courses/18-06sc-linear-algebra-fall-2011/",
    )


def test_import_is_idempotent_and_processes_exactly_one_source(
    repository: CourseRepository,
) -> None:
    first = repository.create_import("import-1", _input(), [("week-1-notes.md", b"# Vectors\n")])
    processed = repository.process_import(first.processing_job.id)
    replay = repository.create_import("import-1", _input(), [("week-1-notes.md", b"# Vectors\n")])

    assert first.created is True
    assert processed.status is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    assert replay.created is False
    assert replay.processing_job.id == first.processing_job.id
    assert len(repository.list_sources(first.course.id)) == 1
    assert len(repository.get_manifest(first.course.id).entries) == 1
    assert first.course.ocw_url == _input().ocw_url

    with pytest.raises(IdempotencyConflictError):
        repository.create_import(
            "import-1",
            _input(),
            [("week-1-notes.md", b"changed")],
        )


def test_concurrent_import_retries_share_one_course_job_and_source(
    repository: CourseRepository,
) -> None:
    def create_once(_: int) -> str:
        worker = CourseRepository(repository.path_service, owner_scope="user-a")
        result = worker.create_import(
            "import-concurrent",
            _input(),
            [("lecture.md", b"lecture")],
        )
        return result.course.id

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(create_once, range(12)))

    assert len(set(ids)) == 1
    course_id = ids[0]
    assert len(repository.list_sources(course_id)) == 1
    assert repository.get_processing_job(course_id) is not None


def test_safe_basename_with_repeated_dots_is_not_treated_as_traversal(
    repository: CourseRepository,
) -> None:
    created = repository.create_import("import-dots", _input(), [("week..1.md", b"notes")])
    assert repository.list_sources(created.course.id)[0].display_filename == "week..1.md"


def test_manifest_requires_unknown_role_and_explicit_solution_confirmation(
    repository: CourseRepository,
) -> None:
    created = repository.create_import(
        "import-2",
        _input(),
        [("week-1-material.md", b"material"), ("week-1-solution.md", b"answer")],
    )
    repository.process_import(created.processing_job.id)
    manifest = repository.get_manifest(created.course.id)
    assert set(manifest.blockers) == {"unknown_role", "suspected_solution_confirmation"}
    with pytest.raises(ManifestApprovalBlockedError):
        repository.approve_manifest(created.course.id, manifest.revision)

    with pytest.raises(ManifestRevisionConflictError):
        repository.update_manifest(created.course.id, manifest.revision + 1, [])

    corrected = repository.update_manifest(
        created.course.id,
        manifest.revision,
        [
            {
                "id": entry.id,
                "role": ManifestRole.READING.value,
                "visibility": ManifestVisibility.LEARNER_VISIBLE.value,
            }
            if entry.role is ManifestRole.UNKNOWN
            else {
                "id": entry.id,
                "role": ManifestRole.SOLUTION.value,
                "visibility": ManifestVisibility.INSTRUCTOR_ONLY.value,
                "role_confirmed": True,
                "visibility_confirmed": True,
            }
            for entry in manifest.entries
        ],
    )
    assert corrected.blockers == ()
    approved = repository.approve_manifest(created.course.id, corrected.revision)
    assert approved.eligible_for_planning is True
    assert repository.get_processing_job(created.course.id).status is CourseJobStatus.COMPLETED


def test_failed_source_stage_is_safe_and_retryable_without_duplicate_rows(
    repository: CourseRepository,
) -> None:
    class FailingAdapter:
        def index_sources(self, **_kwargs: object) -> None:
            raise RuntimeError("provider detail must not escape")

    repository.ingestion_adapter = FailingAdapter()
    created = repository.create_import("import-3", _input(), [("lecture.md", b"lecture")])
    failed = repository.process_import(created.processing_job.id)
    assert failed.status is CourseJobStatus.FAILED
    assert failed.failed_stage == "source_processing"
    assert failed.error_code == "source_processing_failed"
    assert len(repository.list_sources(created.course.id)) == 1

    with pytest.raises(InvalidJobRetryError):
        repository.process_import(created.processing_job.id)


def test_successful_indexing_is_not_repeated_when_manifest_stage_resumes(
    repository: CourseRepository,
) -> None:
    class CountingAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def index_sources(self, **_kwargs: object) -> None:
            self.calls += 1

    adapter = CountingAdapter()
    repository.ingestion_adapter = adapter
    created = repository.create_import("import-resume", _input(), [("lecture.md", b"lecture")])
    first = repository.process_import(created.processing_job.id)
    assert first.status is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    assert adapter.calls == 1

    # Simulate a process dying after source indexing but before its public
    # manifest-stage commit. The durable source_indexed bit lets recovery
    # resume without invoking the existing ingestion seam a second time.
    with repository._connect() as conn:
        conn.execute(
            """
            UPDATE course_processing_jobs
            SET status = 'source_processing', stage = 'source_processing',
                lease_token = NULL, lease_until = NULL
            WHERE id = ?
            """,
            (created.processing_job.id,),
        )
        conn.commit()

    resumed = repository.retry_import(created.processing_job.id)
    assert resumed.status is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    assert adapter.calls == 1
    assert len(repository.get_manifest(created.course.id).entries) == 1


def test_async_ingestion_adapter_is_completed_before_manifest_stage(
    repository: CourseRepository,
) -> None:
    class AsyncAdapter:
        def __init__(self) -> None:
            self.called = False

        async def index_sources(self, **_kwargs: object) -> None:
            self.called = True

    adapter = AsyncAdapter()
    repository.ingestion_adapter = adapter
    created = repository.create_import("import-async", _input(), [("lecture.md", b"lecture")])
    processed = repository.process_import(created.processing_job.id)
    assert processed.status is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    assert adapter.called is True


def test_concurrent_processing_claims_one_source_stage(
    repository: CourseRepository,
) -> None:
    from threading import Lock
    import time

    class CountingAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self._lock = Lock()

        def index_sources(self, **_kwargs: object) -> None:
            with self._lock:
                self.calls += 1
            time.sleep(0.03)

    adapter = CountingAdapter()
    repository.ingestion_adapter = adapter
    created = repository.create_import(
        "import-process-concurrent", _input(), [("lecture.md", b"lecture")]
    )

    def process_once(_: int) -> CourseJobStatus:
        worker = CourseRepository(
            repository.path_service, owner_scope="user-a", ingestion_adapter=adapter
        )
        return worker.process_import(created.processing_job.id).status

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(process_once, range(8)))

    assert set(statuses) <= {
        CourseJobStatus.SOURCE_PROCESSING,
        CourseJobStatus.AWAITING_MANIFEST_REVIEW,
    }
    assert (
        repository.get_processing_job(created.course.id).status
        is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    )
    assert adapter.calls == 1
