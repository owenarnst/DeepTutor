import time

import pytest

from deeptutor.learning.models import (
    ErrorRecord,
    ErrorType,
    KnowledgeType,
    LearningProgress,
    RepetitionState,
    ReviewTask,
)
from deeptutor.learning.scheduler import INTERVAL_SEQUENCES, SpacedRepetitionScheduler


@pytest.fixture
def scheduler():
    return SpacedRepetitionScheduler()


# ── interval sequences ───────────────────────────────────────────────────


class TestIntervalSequences:
    def test_memory_sequence(self):
        assert INTERVAL_SEQUENCES[KnowledgeType.MEMORY] == [0, 1, 3, 7, 14, 30, 60]

    def test_concept_sequence(self):
        assert INTERVAL_SEQUENCES[KnowledgeType.CONCEPT] == [3, 7, 14, 30]

    def test_procedure_sequence(self):
        assert INTERVAL_SEQUENCES[KnowledgeType.PROCEDURE] == [3, 7, 14]

    def test_design_sequence(self):
        assert INTERVAL_SEQUENCES[KnowledgeType.DESIGN] == [14, 28]


# ── get_initial_state ────────────────────────────────────────────────────


class TestInitialState:
    def test_memory_initial(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        assert state.interval_index == 0
        assert state.consecutive_correct == 0
        assert state.consecutive_wrong == 0
        assert abs(state.next_review_at - time.time()) < 5

    def test_design_initial(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.DESIGN)
        assert state.interval_index == 0
        assert abs(state.next_review_at - time.time() - 14 * 86400) < 5

    def test_mutable_debug_mode_changes_initial_and_subsequent_intervals(self, scheduler):
        scheduler.DEBUG_MODE = False
        normal_initial = scheduler.get_initial_state(KnowledgeType.DESIGN)
        assert scheduler.DEBUG_MODE is False
        assert abs(normal_initial.next_review_at - time.time() - 14 * 86400) < 5

        scheduler.DEBUG_MODE = True
        debug_initial = scheduler.get_initial_state(KnowledgeType.DESIGN)
        assert scheduler.DEBUG_MODE is True
        assert abs(debug_initial.next_review_at - time.time() - 14) < 5

        debug_state = scheduler.schedule_next(
            RepetitionState(next_review_at=time.time()),
            KnowledgeType.DESIGN,
            True,
        )
        assert abs(debug_state.next_review_at - time.time() - 28) < 5

        scheduler.DEBUG_MODE = False
        normal_state = scheduler.schedule_next(
            RepetitionState(next_review_at=time.time()),
            KnowledgeType.DESIGN,
            True,
        )
        assert abs(normal_state.next_review_at - time.time() - 28 * 86400) < 5


# ── schedule_next: correct advances ──────────────────────────────────────


class TestCorrectAdvances:
    def test_first_correct(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, True)
        assert state.interval_index == 1
        assert state.consecutive_correct == 1
        assert state.consecutive_wrong == 0

    def test_two_consecutive_skip(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, True)  # idx=1, cc=1
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, True)  # idx=3, cc=0
        assert state.interval_index == 3
        assert state.consecutive_correct == 0


# ── schedule_next: wrong retreats ────────────────────────────────────────


class TestWrongRetreats:
    def test_wrong_decrements(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, True)  # idx=1
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, False)  # idx=0
        assert state.interval_index == 0
        assert state.consecutive_wrong == 1
        assert state.consecutive_correct == 0

    def test_two_consecutive_wrong_resets(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, True)  # idx=1
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, False)  # idx=0, cw=1
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, False)  # idx=0, cw resets
        assert state.consecutive_wrong == 0


# ── schedule_next: boundaries ────────────────────────────────────────────


class TestBoundaries:
    def test_cant_go_below_zero(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, False)
        assert state.interval_index == 0

    def test_cant_exceed_sequence(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.MEMORY)
        state.interval_index = 6  # max for MEMORY
        state = scheduler.schedule_next(state, KnowledgeType.MEMORY, True)
        assert state.interval_index == 6


# ── different types ──────────────────────────────────────────────────────


class TestDifferentTypes:
    def test_design_first_correct(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.DESIGN)
        state = scheduler.schedule_next(state, KnowledgeType.DESIGN, True)
        assert state.interval_index == 1

    def test_concept_sequence(self, scheduler):
        state = scheduler.get_initial_state(KnowledgeType.CONCEPT)
        state = scheduler.schedule_next(state, KnowledgeType.CONCEPT, True)
        assert state.interval_index == 1


# ── get_due_tasks ────────────────────────────────────────────────────────


class TestGetDueTasks:
    def test_returns_due_only(self, scheduler):
        now = time.time()
        state = RepetitionState(next_review_at=now - 10)
        task = ReviewTask(
            id="r1",
            knowledge_point_id="kp1",
            knowledge_type=KnowledgeType.MEMORY,
            due_at=now - 10,
            priority=1,
            state=state,
        )
        lp = LearningProgress(book_id="b1", review_queue=[task])
        due = scheduler.get_due_tasks(lp)
        assert len(due) == 1

    def test_skips_future(self, scheduler):
        now = time.time()
        state = RepetitionState(next_review_at=now + 86400)
        task = ReviewTask(
            id="r1",
            knowledge_point_id="kp1",
            knowledge_type=KnowledgeType.MEMORY,
            due_at=now + 86400,
            priority=1,
            state=state,
        )
        lp = LearningProgress(book_id="b1", review_queue=[task])
        due = scheduler.get_due_tasks(lp)
        assert len(due) == 0

    def test_sorted_by_priority(self, scheduler):
        now = time.time()
        lp = LearningProgress(book_id="b1")
        lp.review_queue = [
            ReviewTask(
                id="r_low",
                knowledge_point_id="kp_low",
                knowledge_type=KnowledgeType.MEMORY,
                due_at=now - 10,
                priority=5,
                state=RepetitionState(next_review_at=now - 10),
            ),
            ReviewTask(
                id="r_high",
                knowledge_point_id="kp_high",
                knowledge_type=KnowledgeType.MEMORY,
                due_at=now - 10,
                priority=1,
                state=RepetitionState(next_review_at=now - 10),
            ),
        ]
        due = scheduler.get_due_tasks(lp)
        assert [t.id for t in due] == ["r_high", "r_low"]

    def test_respects_max_tasks(self, scheduler):
        now = time.time()
        lp = LearningProgress(book_id="b1")
        lp.review_queue = [
            ReviewTask(
                id=f"r{i}",
                knowledge_point_id=f"kp{i}",
                knowledge_type=KnowledgeType.MEMORY,
                due_at=now - 10,
                priority=i,
                state=RepetitionState(next_review_at=now - 10),
            )
            for i in range(8)
        ]
        due = scheduler.get_due_tasks(lp, max_tasks=3)
        assert len(due) == 3

    def test_duplicate_ids_preserve_sorted_original_tasks_and_states(self, scheduler):
        now = time.time()
        state_one = RepetitionState(next_review_at=now - 20, interval_index=1)
        state_two = RepetitionState(next_review_at=now - 10, interval_index=2)
        task_one = ReviewTask(
            id="duplicate",
            knowledge_point_id="kp-one",
            knowledge_type=KnowledgeType.MEMORY,
            due_at=now - 20,
            priority=4,
            state=state_one,
        )
        task_two = ReviewTask(
            id="duplicate",
            knowledge_point_id="kp-two",
            knowledge_type=KnowledgeType.DESIGN,
            due_at=now - 10,
            priority=1,
            state=state_two,
        )
        progress = LearningProgress(book_id="b1", review_queue=[task_one, task_two])

        due = scheduler.get_due_tasks(progress)
        assert [task.knowledge_point_id for task in due] == ["kp-two", "kp-one"]
        assert due[0] is task_two
        assert due[1] is task_one
        assert due[0].state is state_two
        assert due[1].state is state_one

        reopened = LearningProgress.model_validate_json(progress.model_dump_json())
        reopened_due = scheduler.get_due_tasks(reopened)
        assert [task.knowledge_point_id for task in reopened_due] == ["kp-two", "kp-one"]
        assert reopened_due[0] is reopened.review_queue[1]
        assert reopened_due[1] is reopened.review_queue[0]


# ── build_review_queue ───────────────────────────────────────────────────


class TestBuildReviewQueue:
    def test_queue_reuses_repetition_state_and_stays_synchronized(self, scheduler):
        state = RepetitionState(next_review_at=time.time())
        lp = LearningProgress(book_id="b1")
        lp.repetition_states["kp1"] = state
        lp.knowledge_types["kp1"] = KnowledgeType.MEMORY

        queue = scheduler.build_review_queue(lp)
        assert queue[0].state is state

        scheduler.schedule_next(state, KnowledgeType.MEMORY, True)
        assert queue[0].state is state
        assert queue[0].state.interval_index == state.interval_index == 1

    def test_due_tasks_reuse_queued_task_and_state(self, scheduler):
        state = RepetitionState(next_review_at=time.time() - 10)
        task = ReviewTask(
            id="r1",
            knowledge_point_id="kp1",
            knowledge_type=KnowledgeType.MEMORY,
            due_at=state.next_review_at,
            priority=1,
            state=state,
        )
        lp = LearningProgress(book_id="b1", review_queue=[task])

        due = scheduler.get_due_tasks(lp)
        assert due[0] is task
        assert due[0].state is state

    def test_reopen_then_rebuild_rebinds_queue_to_canonical_state(self, scheduler):
        state = RepetitionState(next_review_at=time.time())
        progress = LearningProgress(book_id="b1")
        progress.repetition_states["kp1"] = state
        progress.knowledge_types["kp1"] = KnowledgeType.MEMORY
        progress.review_queue = scheduler.build_review_queue(progress)

        reopened = LearningProgress.model_validate_json(progress.model_dump_json())
        assert reopened.review_queue[0].state is not reopened.repetition_states["kp1"]

        rebuilt = scheduler.build_review_queue(reopened)
        assert rebuilt[0].state is reopened.repetition_states["kp1"]

    def test_error_records_get_priority_1(self, scheduler):
        now = time.time()
        state = RepetitionState(next_review_at=now)
        lp = LearningProgress(book_id="b1")
        lp.repetition_states["kp1"] = state
        lp.knowledge_types["kp1"] = KnowledgeType.MEMORY
        lp.error_records = [
            ErrorRecord(
                id="e1",
                question_id="q1",
                knowledge_point_id="kp1",
                module_id="m1",
                error_type=ErrorType.APPLICATION_ERROR,
            )
        ]
        tasks = scheduler.build_review_queue(lp)
        assert len(tasks) == 1
        assert tasks[0].priority == 1

    def test_non_error_kp_uses_type_priority(self, scheduler):
        now = time.time()
        lp = LearningProgress(book_id="b1")
        lp.repetition_states["kp_design"] = RepetitionState(next_review_at=now)
        lp.knowledge_types["kp_design"] = KnowledgeType.DESIGN
        tasks = scheduler.build_review_queue(lp)
        assert len(tasks) == 1
        # DESIGN has the lowest urgency -> largest priority number, never 1.
        assert tasks[0].priority == 5
        assert tasks[0].knowledge_type == KnowledgeType.DESIGN

    def test_graduated_error_does_not_promote_priority(self, scheduler):
        now = time.time()
        lp = LearningProgress(book_id="b1")
        lp.repetition_states["kp1"] = RepetitionState(next_review_at=now)
        lp.knowledge_types["kp1"] = KnowledgeType.CONCEPT
        # Only active/retrying error records boost priority to 1.
        lp.error_records = [
            ErrorRecord(
                id="e1",
                question_id="q1",
                knowledge_point_id="kp1",
                module_id="m1",
                error_type=ErrorType.APPLICATION_ERROR,
                status="graduated",
            )
        ]
        tasks = scheduler.build_review_queue(lp)
        assert len(tasks) == 1
        assert tasks[0].priority == 3  # CONCEPT type priority, not 1

    def test_retrying_error_promotes_priority(self, scheduler):
        now = time.time()
        lp = LearningProgress(book_id="b1")
        lp.repetition_states["kp1"] = RepetitionState(next_review_at=now)
        lp.knowledge_types["kp1"] = KnowledgeType.CONCEPT
        lp.error_records = [
            ErrorRecord(
                id="e1",
                question_id="q1",
                knowledge_point_id="kp1",
                module_id="m1",
                error_type=ErrorType.APPLICATION_ERROR,
                status="retrying",
            )
        ]
        tasks = scheduler.build_review_queue(lp)
        assert tasks[0].priority == 1

    def test_defaults_missing_type_to_memory(self, scheduler):
        now = time.time()
        lp = LearningProgress(book_id="b1")
        lp.repetition_states["kp1"] = RepetitionState(next_review_at=now)
        # No entry in knowledge_types -> defaults to MEMORY (priority 2).
        tasks = scheduler.build_review_queue(lp)
        assert len(tasks) == 1
        assert tasks[0].knowledge_type == KnowledgeType.MEMORY
        assert tasks[0].priority == 2
