"""Table-driven conformance proof for the neutral learning-kernel contract."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

import pytest

from deeptutor.learning.kernel import (
    Evidence,
    LearningKernel,
    NextActionDecision,
    ObjectiveIdentity,
    ObjectiveState,
    ProgressionDecision,
    ReviewItem,
    ReviewSchedule,
)
from deeptutor.learning.mastery_adapter import MasteryLearningAdapter
from deeptutor.learning.models import (
    KnowledgePoint,
    KnowledgeType,
    LearningModule,
    LearningProgress,
    QuizAttempt,
    RepetitionState,
    ReviewTask,
)
from deeptutor.learning.tests.course_kernel_fixture import (
    CourseEvidenceRecord,
    CourseLearningAdapter,
    CourseObjectiveRecord,
    CoursePolicy,
    CourseReviewRecord,
    CourseState,
)


@dataclass(frozen=True)
class _ConformanceExpectations:
    states: tuple[ObjectiveState, ...]
    evidence: dict[str, tuple[Evidence, ...]]
    progression: dict[str, ProgressionDecision]
    due_reviews: dict[float, tuple[ReviewItem, ...]]
    next_actions: dict[float, NextActionDecision]


def _mastery_kernel() -> MasteryLearningAdapter:
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
        mastery_levels={"objective-1": 0.8},
        qualitative_mastery={"objective-2": False},
        quiz_attempts=[
            QuizAttempt(
                question_id="q1a",
                knowledge_point_id="objective-1",
                module_id="module-1",
                is_correct=False,
                timestamp=1.0,
            ),
            QuizAttempt(
                question_id="q1b",
                knowledge_point_id="objective-1",
                module_id="module-1",
                is_correct=True,
                timestamp=2.0,
            ),
            QuizAttempt(
                question_id="q2",
                knowledge_point_id="objective-2",
                module_id="module-1",
                is_correct=False,
                timestamp=3.0,
            ),
        ],
        repetition_states={
            "objective-1": RepetitionState(
                interval_index=1,
                consecutive_correct=1,
                next_review_at=4.0,
            ),
            "objective-2": RepetitionState(
                interval_index=0,
                next_review_at=20.0,
            ),
        },
        review_queue=[
            ReviewTask(
                id="review_objective-1",
                knowledge_point_id="objective-1",
                knowledge_type=KnowledgeType.MEMORY,
                due_at=4.0,
                priority=2,
                state=RepetitionState(
                    interval_index=1,
                    consecutive_correct=1,
                    next_review_at=4.0,
                ),
            ),
            ReviewTask(
                id="review_objective-2",
                knowledge_point_id="objective-2",
                knowledge_type=KnowledgeType.CONCEPT,
                due_at=20.0,
                priority=3,
                state=RepetitionState(
                    interval_index=0,
                    next_review_at=20.0,
                ),
            ),
        ],
    )
    return MasteryLearningAdapter(progress)


def _course_kernel() -> CourseLearningAdapter:
    return CourseLearningAdapter(
        CourseState(
            objectives=(
                CourseObjectiveRecord(
                    key="course-objective-1",
                    title="Fractions",
                    unit_key="unit-1",
                    category="lesson",
                    position=0,
                    score=0.8,
                    completed=True,
                ),
                CourseObjectiveRecord(
                    key="course-objective-2",
                    title="Ratios",
                    unit_key="unit-1",
                    category="lesson",
                    position=1,
                    score=0.2,
                ),
            ),
            evidence=(
                CourseEvidenceRecord(
                    objective_key="course-objective-1",
                    kind="submission",
                    outcome="accepted",
                    recorded_at=2.0,
                    source="submission-2",
                ),
                CourseEvidenceRecord(
                    objective_key="course-objective-1",
                    kind="submission",
                    outcome="submitted",
                    recorded_at=1.0,
                    source="submission-1",
                ),
                CourseEvidenceRecord(
                    objective_key="course-objective-2",
                    kind="submission",
                    outcome="partial",
                    recorded_at=3.0,
                    source="submission-3",
                ),
            ),
            reviews=(
                CourseReviewRecord(
                    objective_key="course-objective-1",
                    category="lesson",
                    due_at=4.0,
                    priority=9,
                    interval_index=2,
                ),
                CourseReviewRecord(
                    objective_key="course-objective-2",
                    category="lesson",
                    due_at=8.0,
                    priority=1,
                    interval_index=0,
                ),
            ),
            policy=CoursePolicy(
                mastery_target=0.75,
                review_precedes_active_work=False,
            ),
        )
    )


def _mastery_expectations() -> _ConformanceExpectations:
    return _ConformanceExpectations(
        states=(
            ObjectiveState(
                identity=ObjectiveIdentity(
                    objective_id="objective-1",
                    name="First objective",
                    category="memory",
                    container_id="module-1",
                    position=0,
                ),
                status="learning",
                score=0.8,
                target=0.9,
            ),
            ObjectiveState(
                identity=ObjectiveIdentity(
                    objective_id="objective-2",
                    name="Second objective",
                    category="concept",
                    container_id="module-1",
                    position=1,
                ),
                status="learning",
                score=0.0,
                target=1.0,
            ),
        ),
        evidence={
            "objective-1": (
                Evidence(
                    objective_id="objective-1",
                    kind="assessment",
                    outcome=False,
                    observed_at=1.0,
                    source="q1a",
                    metadata={"module_id": "module-1"},
                ),
                Evidence(
                    objective_id="objective-1",
                    kind="assessment",
                    outcome=True,
                    observed_at=2.0,
                    source="q1b",
                    metadata={"module_id": "module-1"},
                ),
            ),
            "objective-2": (
                Evidence(
                    objective_id="objective-2",
                    kind="judgement",
                    outcome=False,
                    observed_at=0.0,
                    source="qualitative-assessment",
                ),
                Evidence(
                    objective_id="objective-2",
                    kind="assessment",
                    outcome=False,
                    observed_at=3.0,
                    source="q2",
                    metadata={"module_id": "module-1"},
                ),
            ),
        },
        progression={
            "objective-1": ProgressionDecision(
                decision="continue",
                objective_id="objective-1",
                reason="The objective remains below its Mastery gate.",
                status="learning",
            ),
            "objective-2": ProgressionDecision(
                decision="continue",
                objective_id="objective-2",
                reason="The objective remains below its Mastery gate.",
                status="learning",
            ),
        },
        due_reviews={
            5.0: (
                ReviewItem(
                    objective_id="objective-1",
                    category="memory",
                    due_at=4.0,
                    priority=2,
                    interval_index=1,
                    is_due=True,
                    item_id="review_objective-1",
                    schedule=ReviewSchedule(
                        objective_id="objective-1",
                        due_at=4.0,
                        interval_index=1,
                        consecutive_correct=1,
                    ),
                ),
            ),
            25.0: (
                ReviewItem(
                    objective_id="objective-1",
                    category="memory",
                    due_at=4.0,
                    priority=2,
                    interval_index=1,
                    is_due=True,
                    item_id="review_objective-1",
                    schedule=ReviewSchedule(
                        objective_id="objective-1",
                        due_at=4.0,
                        interval_index=1,
                        consecutive_correct=1,
                    ),
                ),
                ReviewItem(
                    objective_id="objective-2",
                    category="concept",
                    due_at=20.0,
                    priority=3,
                    interval_index=0,
                    is_due=True,
                    item_id="review_objective-2",
                    schedule=ReviewSchedule(
                        objective_id="objective-2",
                        due_at=20.0,
                        interval_index=0,
                    ),
                ),
            ),
        },
        next_actions={
            5.0: NextActionDecision(
                action="review",
                objective_id="objective-1",
                reason="This objective is due for spaced-repetition review.",
                status="learning",
                container_id="module-1",
                container_name="Module 1",
                objective_name="First objective",
                objective_category="memory",
                score=0.8,
                target=0.9,
            ),
            25.0: NextActionDecision(
                action="review",
                objective_id="objective-1",
                reason="This objective is due for spaced-repetition review.",
                status="learning",
                container_id="module-1",
                container_name="Module 1",
                objective_name="First objective",
                objective_category="memory",
                score=0.8,
                target=0.9,
            ),
        },
    )


def _course_expectations() -> _ConformanceExpectations:
    return _ConformanceExpectations(
        states=(
            ObjectiveState(
                identity=ObjectiveIdentity(
                    objective_id="course-objective-1",
                    name="Fractions",
                    category="lesson",
                    container_id="unit-1",
                    position=0,
                ),
                status="complete",
                score=0.8,
                target=0.75,
            ),
            ObjectiveState(
                identity=ObjectiveIdentity(
                    objective_id="course-objective-2",
                    name="Ratios",
                    category="lesson",
                    container_id="unit-1",
                    position=1,
                ),
                status="active",
                score=0.2,
                target=0.75,
            ),
        ),
        evidence={
            "course-objective-1": (
                Evidence(
                    objective_id="course-objective-1",
                    kind="submission",
                    outcome="submitted",
                    observed_at=1.0,
                    source="submission-1",
                ),
                Evidence(
                    objective_id="course-objective-1",
                    kind="submission",
                    outcome="accepted",
                    observed_at=2.0,
                    source="submission-2",
                ),
            ),
            "course-objective-2": (
                Evidence(
                    objective_id="course-objective-2",
                    kind="submission",
                    outcome="partial",
                    observed_at=3.0,
                    source="submission-3",
                ),
            ),
        },
        progression={
            "course-objective-1": ProgressionDecision(
                decision="advance",
                objective_id="course-objective-1",
                reason="The Course objective met its independent target.",
                status="complete",
            ),
            "course-objective-2": ProgressionDecision(
                decision="continue",
                objective_id="course-objective-2",
                reason="The Course objective remains below its independent target.",
                status="active",
            ),
        },
        due_reviews={
            5.0: (
                ReviewItem(
                    objective_id="course-objective-1",
                    category="lesson",
                    due_at=4.0,
                    priority=9,
                    interval_index=2,
                    is_due=True,
                    item_id="course-review-course-objective-1",
                    schedule=ReviewSchedule(
                        objective_id="course-objective-1",
                        due_at=4.0,
                        interval_index=2,
                    ),
                ),
            ),
            10.0: (
                ReviewItem(
                    objective_id="course-objective-1",
                    category="lesson",
                    due_at=4.0,
                    priority=9,
                    interval_index=2,
                    is_due=True,
                    item_id="course-review-course-objective-1",
                    schedule=ReviewSchedule(
                        objective_id="course-objective-1",
                        due_at=4.0,
                        interval_index=2,
                    ),
                ),
                ReviewItem(
                    objective_id="course-objective-2",
                    category="lesson",
                    due_at=8.0,
                    priority=1,
                    interval_index=0,
                    is_due=True,
                    item_id="course-review-course-objective-2",
                    schedule=ReviewSchedule(
                        objective_id="course-objective-2",
                        due_at=8.0,
                        interval_index=0,
                    ),
                ),
            ),
        },
        next_actions={
            5.0: NextActionDecision(
                action="work",
                objective_id="course-objective-2",
                objective_name="Ratios",
                objective_category="lesson",
                container_id="unit-1",
                status="active",
                score=0.2,
                target=0.75,
                reason="Continue the active Course objective before scheduled review.",
            ),
            10.0: NextActionDecision(
                action="work",
                objective_id="course-objective-2",
                objective_name="Ratios",
                objective_category="lesson",
                container_id="unit-1",
                status="active",
                score=0.2,
                target=0.75,
                reason="Continue the active Course objective before scheduled review.",
            ),
        },
    )


def assert_learning_kernel_conformance(
    kernel: LearningKernel,
    expected: _ConformanceExpectations,
) -> None:
    """Exact, reusable observable checks for any kernel implementation."""

    assert isinstance(kernel, LearningKernel)

    states = tuple(kernel.objective_states())
    assert states == expected.states
    assert [state.identity.objective_id for state in states] == [
        state.identity.objective_id for state in expected.states
    ]

    for objective_id, expected_evidence in expected.evidence.items():
        evidence = tuple(kernel.evidence_for(objective_id))
        assert evidence == expected_evidence
        assert evidence
        assert all(item.objective_id == objective_id for item in evidence)
        assert [item.observed_at for item in evidence] == sorted(
            item.observed_at for item in evidence
        )

    for objective_id, expected_progression in expected.progression.items():
        assert kernel.progression_decision(objective_id) == expected_progression

    for now, expected_reviews in expected.due_reviews.items():
        reviews = tuple(kernel.due_reviews(now=now))
        assert reviews == expected_reviews
        assert all(item.is_due for item in reviews)
        assert all(item.due_at <= now for item in reviews)
        assert all(item.schedule is not None for item in reviews)

    for now, expected_action in expected.next_actions.items():
        assert kernel.next_action(now=now) == expected_action
        assert expected_action.action
        assert expected_action.reason


@pytest.mark.parametrize(
    ("factory", "expectations"),
    [
        (_mastery_kernel, _mastery_expectations),
        (_course_kernel, _course_expectations),
    ],
    ids=["mastery-adapter", "course-test-adapter"],
)
def test_learning_kernel_conformance(factory, expectations) -> None:
    assert_learning_kernel_conformance(factory(), expectations())


def test_course_policy_is_materially_independent_from_mastery_policy() -> None:
    mastery = _mastery_kernel()
    course = _course_kernel()

    mastery_state = mastery.objective_states()[0]
    course_state = course.objective_states()[0]
    assert mastery_state.target == 0.9
    assert course_state.target == 0.75
    assert mastery_state.status == "learning"
    assert course_state.status == "complete"

    assert mastery.due_reviews(now=10.0)[0].due_at == 4.0
    assert course.due_reviews(now=10.0)[1].due_at == 8.0
    assert mastery.next_action(now=5.0).action == "review"
    assert course.next_action(now=5.0).action == "work"


class _BrokenKernel:
    """Mutation harness proving the oracle rejects shallow implementations."""

    def __init__(self, delegate: LearningKernel, failure: str) -> None:
        self._delegate = delegate
        self._failure = failure

    def objective_states(self):
        states = tuple(self._delegate.objective_states())
        return tuple(reversed(states)) if self._failure == "order" else states

    def evidence_for(self, objective_id: str):
        return () if self._failure == "evidence" else self._delegate.evidence_for(objective_id)

    def progression_decision(self, objective_id: str):
        return self._delegate.progression_decision(objective_id)

    def due_reviews(self, now: float):
        return () if self._failure == "reviews" else self._delegate.due_reviews(now)

    def next_action(self, now: float):
        if self._failure == "action":
            return NextActionDecision(action="nonsense")
        return self._delegate.next_action(now)


@pytest.mark.parametrize("failure", ["evidence", "reviews", "action", "order"])
def test_conformance_oracle_rejects_broken_implementations(failure: str) -> None:
    with pytest.raises(AssertionError):
        assert_learning_kernel_conformance(
            _BrokenKernel(_course_kernel(), failure),
            _course_expectations(),
        )


def test_course_fixture_has_no_mastery_state_or_inheritance() -> None:
    source_path = Path(__file__).with_name("course_kernel_fixture.py")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert "deeptutor.learning.models" not in imported_modules
    assert "deeptutor.learning.mastery_adapter" not in imported_modules
    assert "LearningProgress" not in source
    assert "class CourseLearningAdapter(LearningKernel)" in source


def test_actual_kernel_public_path_does_not_require_product_modules() -> None:
    script = r"""
import importlib.abc
import sys


class BlockProductImports(importlib.abc.MetaPathFinder):
    blocked = (
        "pydantic",
        "deeptutor.learning.models",
        "deeptutor.learning.mastery_adapter",
    )

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.__class__.__name__:
            return None
        if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in self.blocked):
            raise ModuleNotFoundError(fullname)
        return None


sys.meta_path.insert(0, BlockProductImports())
from deeptutor.learning.kernel import Evidence, LearningKernel

assert Evidence and LearningKernel
print("kernel-only-import-ok")
"""
    repo_root = Path(__file__).parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "kernel-only-import-ok"


def test_historical_learning_root_exports_remain_compatible() -> None:
    import deeptutor.learning as learning
    from deeptutor.learning import (
        KnowledgeType,
        LearningProgress,
        MasteryLearningAdapter,
    )
    from deeptutor.learning.mastery_adapter import MasteryLearningAdapter as Adapter
    from deeptutor.learning.models import KnowledgeType as ModelKnowledgeType
    from deeptutor.learning.models import LearningProgress as ModelLearningProgress

    assert KnowledgeType is ModelKnowledgeType
    assert LearningProgress is ModelLearningProgress
    assert MasteryLearningAdapter is Adapter
    assert all(hasattr(learning, name) for name in learning.__all__)


def test_kernel_module_has_no_product_domain_imports() -> None:
    """The shared boundary itself must remain dependency-free."""

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
