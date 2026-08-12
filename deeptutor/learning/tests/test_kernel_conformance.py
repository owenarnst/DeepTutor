"""Reusable conformance proof for the neutral learning-kernel contract.

The second implementation is deliberately test-only: it owns a tiny Course-like
state and policy instead of importing or inheriting from the Mastery models.
The parametrized assertions therefore exercise the shared boundary rather than
making Course production state a prerequisite for the kernel.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from deeptutor.learning.kernel import (
    Evidence,
    LearningKernel,
    NextActionDecision,
    ObjectiveIdentity,
    ObjectiveState,
    ProgressionDecision,
    ReviewItem,
)
from deeptutor.learning.mastery_adapter import MasteryLearningAdapter
from deeptutor.learning.models import (
    KnowledgePoint,
    KnowledgeType,
    LearningModule,
    LearningProgress,
    RepetitionState,
    ReviewTask,
)


def _mastery_kernel() -> LearningKernel:
    progress = LearningProgress(
        book_id="kernel-mastery",
        modules=[
            LearningModule(
                id="module-1",
                name="Module 1",
                order=0,
                knowledge_points=[
                    KnowledgePoint(
                        id="objective-1",
                        name="First objective",
                        type=KnowledgeType.MEMORY,
                        module_id="module-1",
                    ),
                    KnowledgePoint(
                        id="objective-2",
                        name="Second objective",
                        type=KnowledgeType.CONCEPT,
                        module_id="module-1",
                    ),
                ],
            )
        ],
        mastery_levels={"objective-1": 0.95},
        repetition_states={
            "objective-1": RepetitionState(
                interval_index=1,
                consecutive_correct=1,
                next_review_at=1.0,
            )
        },
        review_queue=[
            ReviewTask(
                id="review_objective-1",
                knowledge_point_id="objective-1",
                knowledge_type=KnowledgeType.MEMORY,
                due_at=1.0,
                priority=2,
                state=RepetitionState(
                    interval_index=1,
                    consecutive_correct=1,
                    next_review_at=1.0,
                ),
            )
        ],
    )
    return MasteryLearningAdapter(progress)


@dataclass
class _CourseKernel:
    """Independent test-only implementation with a different policy."""

    _objectives: tuple[ObjectiveState, ...] = (
        ObjectiveState(
            identity=ObjectiveIdentity(
                objective_id="objective-1",
                name="First objective",
                category="lesson",
                container_id="unit-1",
                position=0,
            ),
            status="complete",
            score=1.0,
            target=0.8,
        ),
        ObjectiveState(
            identity=ObjectiveIdentity(
                objective_id="objective-2",
                name="Second objective",
                category="lesson",
                container_id="unit-1",
                position=1,
            ),
            status="active",
            score=0.5,
            target=0.8,
        ),
    )
    _evidence: dict[str, tuple[Evidence, ...]] = field(
        default_factory=lambda: {
            "objective-1": (
                Evidence(
                    objective_id="objective-1",
                    kind="submission",
                    outcome="accepted",
                    observed_at=1.0,
                ),
            )
        }
    )
    _reviews: tuple[ReviewItem, ...] = (
        ReviewItem(
            objective_id="objective-1",
            category="lesson",
            due_at=1.0,
            priority=7,
            interval_index=0,
        ),
    )

    def objective_states(self) -> tuple[ObjectiveState, ...]:
        return self._objectives

    def evidence_for(self, objective_id: str) -> tuple[Evidence, ...]:
        return self._evidence.get(objective_id, ())

    def progression_decision(self, objective_id: str) -> ProgressionDecision:
        state = next(
            item for item in self._objectives if item.identity.objective_id == objective_id
        )
        if state.status == "complete":
            return ProgressionDecision(
                decision="advance",
                objective_id=objective_id,
                reason="The submitted work met the Course unit target.",
            )
        return ProgressionDecision(
            decision="continue",
            objective_id=objective_id,
            reason="The Course unit target has not been met.",
        )

    def due_reviews(self, now: float) -> tuple[ReviewItem, ...]:
        return tuple(item for item in self._reviews if item.due_at <= now)

    def next_action(self, now: float) -> NextActionDecision:
        due = self.due_reviews(now)
        if due:
            return NextActionDecision(
                action="review",
                objective_id=due[0].objective_id,
                reason="Course policy schedules this lesson for review.",
            )
        active = next((item for item in self._objectives if item.status != "complete"), None)
        if active is None:
            return NextActionDecision(action="complete", reason="All Course lessons are complete.")
        return NextActionDecision(
            action="work",
            objective_id=active.identity.objective_id,
            reason="Continue the active Course lesson.",
        )


def assert_learning_kernel_conformance(kernel: LearningKernel) -> None:
    """Run the shared observable checks for any product adapter."""

    assert isinstance(kernel, LearningKernel)

    states = kernel.objective_states()
    assert states
    assert all(state.identity.objective_id for state in states)
    assert all(state.identity.name for state in states)
    assert all(state.status for state in states)

    first_id = states[0].identity.objective_id
    evidence = kernel.evidence_for(first_id)
    assert all(item.objective_id == first_id for item in evidence)

    progression = kernel.progression_decision(first_id)
    assert progression.objective_id == first_id
    assert progression.decision in {"advance", "continue", "complete"}
    assert progression.reason

    due = kernel.due_reviews(now=10.0)
    assert all(item.due_at <= 10.0 for item in due)
    assert all(item.objective_id for item in due)

    next_action = kernel.next_action(now=10.0)
    assert next_action.action
    assert next_action.reason


@pytest.mark.parametrize(
    "factory",
    [_mastery_kernel, _CourseKernel],
    ids=["mastery-adapter", "course-test-implementation"],
)
def test_learning_kernel_conformance(factory) -> None:
    assert_learning_kernel_conformance(factory())


def test_course_conformance_policy_is_independent_from_mastery_policy() -> None:
    mastery = _mastery_kernel()
    course = _CourseKernel()

    mastery_next = mastery.next_action(now=10.0)
    course_next = course.next_action(now=10.0)

    assert mastery_next.action == "review"
    assert course_next.action == "review"
    assert mastery_next.objective_id == course_next.objective_id == "objective-1"
    assert mastery.due_reviews(now=10.0)[0].priority != course.due_reviews(now=10.0)[0].priority


def test_kernel_module_has_no_product_domain_imports() -> None:
    """The shared boundary must remain importable without either product."""

    source_path = Path(__file__).parents[1] / "kernel.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ]
    imported_names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert all(not name.startswith("deeptutor") for name in imported_modules)
    assert all(not name.startswith("deeptutor") for name in imported_names)
