from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path

import pytest

from deeptutor.course_mode.models import CourseJobStatus, ManifestRole, ManifestVisibility
from deeptutor.course_mode.repository import (
    CourseInput,
    CourseRepository,
    IdempotencyConflictError,
    InvalidJobRetryError,
    InvalidManifestError,
    InvalidManifestStateError,
    ManifestApprovalBlockedError,
    ManifestRevisionConflictError,
)
from deeptutor.course_mode.source_processing import CourseUpload, DefaultCourseIngestionAdapter
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


def test_idempotent_response_loss_replay_skips_expensive_extraction(
    repository: CourseRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = repository.create_import(
        "import-no-reparse",
        _input(),
        [("lecture.md", b"Vectors are independent directions.")],
    )

    import deeptutor.course_mode.repository as repository_module

    def extraction_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("parser-backed extraction repeated a known idempotent retry")

    monkeypatch.setattr(repository_module, "validate_upload_batch", extraction_must_not_run)
    replay = repository.create_import(
        "import-no-reparse",
        _input(),
        [("lecture.md", b"Vectors are independent directions.")],
    )

    assert replay.created is False
    assert replay.course.id == first.course.id


def test_queued_job_can_be_resumed_after_process_restart(
    repository: CourseRepository,
) -> None:
    class CountingAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def index_sources(self, **_kwargs: object) -> None:
            self.calls += 1

    adapter = CountingAdapter()
    created = repository.create_import(
        "import-queued-recovery",
        _input(),
        [("lecture.md", b"Vectors are independent directions.")],
    )
    assert created.processing_job.status is CourseJobStatus.QUEUED

    reopened = CourseRepository(
        repository.path_service,
        owner_scope="user-a",
        ingestion_adapter=adapter,
    )
    resumed = reopened.retry_import(created.processing_job.id)

    assert resumed.status is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    assert adapter.calls == 1
    assert len(reopened.get_manifest(created.course.id).entries) == 1


def test_stream_upload_handoff_reads_each_source_in_bounded_chunks(
    repository: CourseRepository,
) -> None:
    class BoundedReader(io.BytesIO):
        def __init__(self, content: bytes) -> None:
            super().__init__(content)
            self.max_read_size = 0
            self.unbounded_reads = 0

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                self.unbounded_reads += 1
            self.max_read_size = max(self.max_read_size, size)
            return super().read(size)

    reader = BoundedReader(b"Vectors are independent directions.")
    created = repository.create_import(
        "import-stream-handoff",
        _input(),
        [CourseUpload(filename="lecture.md", stream=reader)],
    )

    assert created.processing_job.status is CourseJobStatus.QUEUED
    assert reader.unbounded_reads == 0
    assert reader.max_read_size <= 1024 * 1024


def test_concurrent_queued_retries_share_one_recovery_claim(
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
    created = repository.create_import(
        "import-queued-concurrent-recovery",
        _input(),
        [("lecture.md", b"Vectors are independent directions.")],
    )

    def retry_once(_: int) -> CourseJobStatus | InvalidJobRetryError:
        worker = CourseRepository(
            repository.path_service,
            owner_scope="user-a",
            ingestion_adapter=adapter,
        )

        try:
            return worker.retry_import(created.processing_job.id).status
        except InvalidJobRetryError as exc:
            # A caller that arrives after the recovery lease has completed
            # observes the non-failed state and must be rejected safely rather
            # than starting a second indexing pass.
            return exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(retry_once, range(12)))

    completed_statuses = [status for status in statuses if isinstance(status, CourseJobStatus)]
    retry_errors = [status for status in statuses if isinstance(status, InvalidJobRetryError)]
    assert set(completed_statuses) <= {
        CourseJobStatus.SOURCE_PROCESSING,
        CourseJobStatus.AWAITING_MANIFEST_REVIEW,
    }
    assert retry_errors
    assert (
        repository.get_processing_job(created.course.id).status
        is CourseJobStatus.AWAITING_MANIFEST_REVIEW
    )
    assert adapter.calls == 1
    assert len(repository.get_manifest(created.course.id).entries) == 1


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


def test_manifest_mutations_require_active_review_and_complete_inventory(
    repository: CourseRepository,
) -> None:
    created = repository.create_import(
        "import-state-guards",
        _input(),
        [("lecture.md", b"lecture"), ("reading.md", b"reading")],
    )

    with pytest.raises(InvalidManifestStateError):
        repository.approve_manifest(created.course.id, 0)
    with pytest.raises(InvalidManifestStateError):
        repository.update_manifest(created.course.id, 0, [])

    repository.process_import(created.processing_job.id)
    manifest = repository.get_manifest(created.course.id)
    with pytest.raises(InvalidManifestError, match="exactly one entry"):
        repository.update_manifest(
            created.course.id,
            manifest.revision,
            [{"id": manifest.entries[0].id, "role": ManifestRole.READING.value}],
        )


def test_failed_manifest_cannot_be_approved_or_edited(
    repository: CourseRepository,
) -> None:
    class FailingAdapter:
        def index_sources(self, **_kwargs: object) -> None:
            raise RuntimeError("provider detail must not escape")

    repository.ingestion_adapter = FailingAdapter()
    created = repository.create_import(
        "import-failed-manifest", _input(), [("lecture.md", b"lecture")]
    )
    failed = repository.process_import(created.processing_job.id)
    assert failed.status is CourseJobStatus.FAILED

    with pytest.raises(InvalidManifestStateError):
        repository.approve_manifest(created.course.id, 0)
    with pytest.raises(InvalidManifestStateError):
        repository.update_manifest(created.course.id, 0, [])


def test_completed_manifest_is_immutable_and_approval_is_not_repeatable(
    repository: CourseRepository,
) -> None:
    created = repository.create_import(
        "import-completed-guards", _input(), [("lecture.md", b"lecture")]
    )
    repository.process_import(created.processing_job.id)
    manifest = repository.get_manifest(created.course.id)
    corrected = repository.update_manifest(
        created.course.id,
        manifest.revision,
        [
            {
                "id": manifest.entries[0].id,
                "role": ManifestRole.LECTURE_NOTE.value,
                "visibility": ManifestVisibility.LEARNER_VISIBLE.value,
            }
        ],
    )
    approved = repository.approve_manifest(created.course.id, corrected.revision)
    assert approved.eligible_for_planning is True

    with pytest.raises(InvalidManifestStateError):
        repository.approve_manifest(created.course.id, approved.revision)
    with pytest.raises(InvalidManifestStateError):
        repository.update_manifest(
            created.course.id,
            approved.revision,
            [{"id": manifest.entries[0].id, "role": ManifestRole.READING.value}],
        )


def test_approval_rejects_empty_or_missing_manifest_entries(
    repository: CourseRepository,
) -> None:
    created = repository.create_import(
        "import-missing-entry", _input(), [("lecture.md", b"lecture")]
    )
    repository.process_import(created.processing_job.id)
    with repository._connect() as conn:
        conn.execute(
            "DELETE FROM course_manifest_entries WHERE course_id = ?",
            (created.course.id,),
        )
        conn.commit()

    manifest = repository.get_manifest(created.course.id)
    assert manifest.entries == ()
    with pytest.raises(InvalidManifestError, match="one entry per accepted source"):
        repository.approve_manifest(created.course.id, manifest.revision)


def test_course_adapter_uses_private_rag_namespace_and_sanitizes_retrieval(
    repository: CourseRepository,
) -> None:
    calls: dict[str, object] = {}

    class FakeRagService:
        async def initialize(self, **kwargs: object) -> bool:
            calls["initialize"] = kwargs
            return True

        async def search(self, **kwargs: object) -> dict[str, object]:
            calls["search"] = kwargs
            return {
                "query": kwargs["query"],
                "answer": "private retrieval result",
                "sources": [{"source": "/server/private/course/lecture.md"}],
            }

    def factory(**kwargs: object) -> FakeRagService:
        calls["factory"] = kwargs
        return FakeRagService()

    adapter = DefaultCourseIngestionAdapter(
        repository.path_service,
        rag_service_factory=factory,
    )
    repository.ingestion_adapter = adapter
    created = repository.create_import(
        "import-private-rag",
        _input(),
        [("lecture.md", b"Vectors are independent directions.")],
    )
    processed = repository.process_import(created.processing_job.id)
    assert processed.status is CourseJobStatus.AWAITING_MANIFEST_REVIEW

    initialize = calls["initialize"]
    assert isinstance(initialize, dict)
    private_root = repository.path_service.get_course_workspace(created.course.id) / "private-index"
    assert initialize["kb_name"] == created.course.id
    assert initialize["file_paths"] == [
        str(
            repository.path_service.get_course_workspace(created.course.id)
            / "sources"
            / "lecture.md"
        )
    ]
    assert calls["factory"] == {"kb_base_dir": private_root, "provider": "llamaindex"}

    result = asyncio.run(
        adapter.search(
            course_id=created.course.id,
            query="independent directions",
            workspace=repository.path_service.get_course_workspace(created.course.id),
        )
    )
    assert result["answer"] == "private retrieval result"
    assert result["sources"] == [{"source": "lecture.md"}]
    assert calls["search"] == {
        "query": "independent directions",
        "kb_name": created.course.id,
        "top_k": 5,
    }
    assert not (private_root / "kb_config.json").exists()


def test_default_adapter_fallback_is_searchable_and_course_private(
    repository: CourseRepository,
) -> None:
    user_kb_root = repository.path_service.get_knowledge_bases_root()
    user_kb_root.mkdir(parents=True)
    sentinel = user_kb_root / "kb_config.json"
    sentinel.write_text('{"knowledge_bases": {"user-kb": {}}}', encoding="utf-8")

    def unavailable_rag(**_kwargs: object) -> object:
        raise ModuleNotFoundError("llama_index")

    adapter = DefaultCourseIngestionAdapter(
        repository.path_service,
        rag_service_factory=unavailable_rag,
    )
    repository.ingestion_adapter = adapter
    created = repository.create_import(
        "import-private-fallback",
        _input(),
        [("lecture.md", b"Vectors are independent directions.")],
    )
    repository.process_import(created.processing_job.id)
    result = asyncio.run(
        adapter.search(
            course_id=created.course.id,
            query="independent directions",
            workspace=repository.path_service.get_course_workspace(created.course.id),
        )
    )

    assert "independent directions" in result["answer"]
    assert result["provider"] == "course-private-lexical"
    assert result["sources"] == [
        {
            "title": "lecture.md",
            "source": "lecture.md",
            "content": "Vectors are independent directions.",
        }
    ]
    assert sentinel.read_text(encoding="utf-8") == '{"knowledge_bases": {"user-kb": {}}}'
    private_index = (
        repository.path_service.get_course_workspace(created.course.id)
        / "private-index"
        / "search-index.json"
    )
    assert private_index.is_file()
