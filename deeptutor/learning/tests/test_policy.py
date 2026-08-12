"""Tests for the Mastery Path policy — the per-type gate and the gate-driven
"what's next" decision that replaced the old linear stage march.

These assert the two Alpha-style principles the old engine violated:

* a HARD gate — an objective is not mastered (and never advanced past) until
  its evidence clears the threshold;
* compression — an already-proven objective is skipped, never re-taught.
"""

from __future__ import annotations

import time

from deeptutor.learning import policy
from deeptutor.learning.models import (
    KnowledgePoint,
    KnowledgeType,
    LearningModule,
    LearningProgress,
    PendingQuestion,
    RepetitionState,
    ReviewTask,
)


def _progress(*kps: KnowledgePoint) -> LearningProgress:
    progress = LearningProgress(book_id="b1")
    progress.modules = [LearningModule(id="m1", name="M1", order=0, knowledge_points=list(kps))]
    progress.current_module_id = "m1"
    for kp in kps:
        progress.knowledge_types[kp.id] = kp.type
    return progress


def _kp(kp_id: str, kp_type: KnowledgeType, name: str = "") -> KnowledgePoint:
    return KnowledgePoint(id=kp_id, name=name or kp_id, type=kp_type, module_id="m1")


# ── per-type gate ──────────────────────────────────────────────────────────


def test_memory_gate_requires_high_quantitative_mastery():
    kp = _kp("kp1", KnowledgeType.MEMORY)
    progress = _progress(kp)
    progress.mastery_levels["kp1"] = 0.8
    assert policy.is_mastered(progress, kp) is False
    progress.mastery_levels["kp1"] = 0.9
    assert policy.is_mastered(progress, kp) is True


def test_procedure_gate_uses_same_quantitative_bar():
    kp = _kp("kp1", KnowledgeType.PROCEDURE)
    progress = _progress(kp)
    progress.mastery_levels["kp1"] = 0.89
    assert policy.is_mastered(progress, kp) is False


def test_concept_gate_is_qualitative_not_quantitative():
    """A high accuracy score must NOT unlock a concept — only the qualitative
    flag does (a concept is gated by an explanation, not string matching)."""
    kp = _kp("kp1", KnowledgeType.CONCEPT)
    progress = _progress(kp)
    progress.mastery_levels["kp1"] = 1.0  # accuracy is high…
    assert policy.is_mastered(progress, kp) is False  # …but the gate is qualitative
    progress.qualitative_mastery["kp1"] = True
    assert policy.is_mastered(progress, kp) is True


def test_objective_status_new_learning_mastered():
    kp = _kp("kp1", KnowledgeType.MEMORY)
    progress = _progress(kp)
    assert policy.objective_status(progress, kp) == "new"
    from deeptutor.learning.models import QuizAttempt

    progress.quiz_attempts.append(
        QuizAttempt(question_id="q", knowledge_point_id="kp1", is_correct=False)
    )
    assert policy.objective_status(progress, kp) == "learning"
    progress.mastery_levels["kp1"] = 0.95
    assert policy.objective_status(progress, kp) == "mastered"


# ── next_objective: gate is the cursor, mastered objectives are skipped ─────


def test_next_objective_skips_mastered_and_returns_first_open():
    kp1, kp2 = _kp("kp1", KnowledgeType.MEMORY), _kp("kp2", KnowledgeType.MEMORY)
    progress = _progress(kp1, kp2)
    progress.mastery_levels["kp1"] = 0.95  # already proven -> compression
    step = policy.next_objective(progress)
    assert step.knowledge_point_id == "kp2"
    assert step.action == "probe"


def test_next_objective_new_is_probe_then_practice_when_seen():
    kp = _kp("kp1", KnowledgeType.PROCEDURE)
    progress = _progress(kp)
    assert policy.next_objective(progress).action == "probe"
    from deeptutor.learning.models import QuizAttempt

    progress.quiz_attempts.append(
        QuizAttempt(question_id="q", knowledge_point_id="kp1", is_correct=False)
    )
    assert policy.next_objective(progress).action == "practice"


def test_next_objective_qualitative_type_recommends_assess():
    kp = _kp("kp1", KnowledgeType.DESIGN)
    progress = _progress(kp)
    progress.qualitative_mastery["kp1"] = False  # seen but not passed
    assert policy.next_objective(progress).action == "assess"


def test_next_objective_pending_question_takes_precedence():
    kp = _kp("kp1", KnowledgeType.MEMORY)
    progress = _progress(kp)
    progress.pending_question = PendingQuestion(
        question_id="q1", knowledge_point_id="kp1", prompt="?", expected_answer="x"
    )
    step = policy.next_objective(progress)
    assert step.action == "answer_pending"
    assert step.pending_prompt == "?"


def test_next_objective_due_review_beats_new_ground():
    kp1, kp2 = _kp("kp1", KnowledgeType.MEMORY), _kp("kp2", KnowledgeType.MEMORY)
    progress = _progress(kp1, kp2)
    progress.mastery_levels["kp1"] = 0.95  # mastered, but due for review
    progress.review_queue = [
        ReviewTask(
            id="r1",
            knowledge_point_id="kp1",
            knowledge_type=KnowledgeType.MEMORY,
            due_at=time.time() - 10,
            priority=1,
            state=RepetitionState(next_review_at=time.time() - 10),
        )
    ]
    step = policy.next_objective(progress)
    assert step.action == "review"
    assert step.knowledge_point_id == "kp1"


def test_due_reviews_reuses_queued_task_and_state():
    now = time.time()
    state = RepetitionState(next_review_at=now - 10)
    task = ReviewTask(
        id="r1",
        knowledge_point_id="kp1",
        knowledge_type=KnowledgeType.MEMORY,
        due_at=state.next_review_at,
        priority=1,
        state=state,
    )
    progress = LearningProgress(book_id="b1", review_queue=[task])

    due = policy.due_reviews(progress, now=now)
    assert due[0] is task
    assert due[0].state is state


def test_due_reviews_preserves_duplicate_id_positions_and_states_after_reopen():
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

    due = policy.due_reviews(progress, now=now)
    assert [task.knowledge_point_id for task in due] == ["kp-two", "kp-one"]
    assert due[0] is task_two
    assert due[1] is task_one
    assert due[0].state is state_two
    assert due[1].state is state_one

    reopened = LearningProgress.model_validate_json(progress.model_dump_json())
    reopened_due = policy.due_reviews(reopened, now=now)
    assert [task.knowledge_point_id for task in reopened_due] == ["kp-two", "kp-one"]
    assert reopened_due[0] is reopened.review_queue[1]
    assert reopened_due[1] is reopened.review_queue[0]


def test_next_objective_complete_when_all_mastered():
    kp = _kp("kp1", KnowledgeType.MEMORY)
    progress = _progress(kp)
    progress.mastery_levels["kp1"] = 0.95
    assert policy.next_objective(progress).action == "complete"


# ── map_summary ─────────────────────────────────────────────────────────────


def test_map_summary_counts_and_completion():
    kp1, kp2 = _kp("kp1", KnowledgeType.MEMORY), _kp("kp2", KnowledgeType.CONCEPT)
    progress = _progress(kp1, kp2)
    progress.mastery_levels["kp1"] = 0.95
    summary = policy.map_summary(progress)
    assert summary["counts"] == {"mastered": 1, "learning": 0, "new": 1, "total": 2}
    assert summary["complete"] is False
    progress.qualitative_mastery["kp2"] = True
    assert policy.map_summary(progress)["complete"] is True
