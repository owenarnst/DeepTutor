"""Independent, test-only Course state and adapter for kernel conformance.

This module intentionally owns Course-native records instead of reusing
Mastery models or neutral DTOs as storage.  Only the adapter methods translate
those records into the public learning-kernel values.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class CourseObjectiveRecord:
    key: str
    title: str
    unit_key: str
    category: str
    position: int
    score: float
    completed: bool = False


@dataclass(frozen=True)
class CourseEvidenceRecord:
    objective_key: str
    kind: str
    outcome: str
    recorded_at: float
    source: str


@dataclass(frozen=True)
class CourseReviewRecord:
    objective_key: str
    category: str
    due_at: float
    priority: int
    interval_index: int


@dataclass(frozen=True)
class CoursePolicy:
    """Course-owned policy deliberately unlike Mastery's policy."""

    mastery_target: float = 0.75
    review_precedes_active_work: bool = False


@dataclass(frozen=True)
class CourseState:
    objectives: tuple[CourseObjectiveRecord, ...]
    evidence: tuple[CourseEvidenceRecord, ...]
    reviews: tuple[CourseReviewRecord, ...]
    policy: CoursePolicy = CoursePolicy()


class CourseLearningAdapter(LearningKernel):
    """Translate independent Course-native state into kernel contracts."""

    def __init__(self, state: CourseState) -> None:
        self._state = state

    def objective_states(self) -> tuple[ObjectiveState, ...]:
        return tuple(
            ObjectiveState(
                identity=ObjectiveIdentity(
                    objective_id=objective.key,
                    name=objective.title,
                    category=objective.category,
                    container_id=objective.unit_key,
                    position=objective.position,
                ),
                status="complete" if self._is_complete(objective) else "active",
                score=objective.score,
                target=self._state.policy.mastery_target,
            )
            for objective in self._state.objectives
        )

    def evidence_for(self, objective_id: str) -> tuple[Evidence, ...]:
        records = sorted(
            (record for record in self._state.evidence if record.objective_key == objective_id),
            key=lambda record: record.recorded_at,
        )
        return tuple(
            Evidence(
                objective_id=record.objective_key,
                kind=record.kind,
                outcome=record.outcome,
                observed_at=record.recorded_at,
                source=record.source,
            )
            for record in records
        )

    def progression_decision(self, objective_id: str) -> ProgressionDecision:
        objective = self._objective(objective_id)
        mastered = self._is_complete(objective)
        if mastered:
            return ProgressionDecision(
                decision="advance",
                objective_id=objective_id,
                reason="The Course objective met its independent target.",
                status="complete",
            )
        return ProgressionDecision(
            decision="continue",
            objective_id=objective_id,
            reason="The Course objective remains below its independent target.",
            status="active",
        )

    def due_reviews(self, now: float) -> tuple[ReviewItem, ...]:
        due = [record for record in self._state.reviews if record.due_at <= now]
        # Course schedules by due time and then its own priority direction;
        # this intentionally differs from Mastery's priority-first queue.
        due.sort(key=lambda record: (record.due_at, -record.priority))
        return tuple(self._review_item(record, is_due=True) for record in due)

    def next_action(self, now: float) -> NextActionDecision:
        active = next(
            (objective for objective in self._state.objectives if not self._is_complete(objective)),
            None,
        )
        due = self.due_reviews(now)
        if self._state.policy.review_precedes_active_work and due:
            return self._review_action(due[0])
        if active is not None:
            return NextActionDecision(
                action="work",
                objective_id=active.key,
                objective_name=active.title,
                objective_category=active.category,
                container_id=active.unit_key,
                status="active",
                score=active.score,
                target=self._state.policy.mastery_target,
                reason="Continue the active Course objective before scheduled review.",
            )
        if due:
            return self._review_action(due[0])
        return NextActionDecision(
            action="complete",
            reason="All Course objectives are complete and no reviews are due.",
        )

    def _objective(self, objective_id: str) -> CourseObjectiveRecord:
        for objective in self._state.objectives:
            if objective.key == objective_id:
                return objective
        raise KeyError(objective_id)

    def _is_complete(self, objective: CourseObjectiveRecord) -> bool:
        return objective.completed or objective.score >= self._state.policy.mastery_target

    def _review_item(self, record: CourseReviewRecord, *, is_due: bool) -> ReviewItem:
        return ReviewItem(
            objective_id=record.objective_key,
            category=record.category,
            due_at=record.due_at,
            priority=record.priority,
            interval_index=record.interval_index,
            is_due=is_due,
            item_id=f"course-review-{record.objective_key}",
            schedule=ReviewSchedule(
                objective_id=record.objective_key,
                due_at=record.due_at,
                interval_index=record.interval_index,
            ),
        )

    def _review_action(self, item: ReviewItem) -> NextActionDecision:
        objective = self._objective(item.objective_id)
        return NextActionDecision(
            action="review",
            objective_id=objective.key,
            objective_name=objective.title,
            objective_category=objective.category,
            container_id=objective.unit_key,
            status="complete" if self._is_complete(objective) else "active",
            score=objective.score,
            target=self._state.policy.mastery_target,
            reason="The Course scheduler selected this due objective.",
        )


__all__ = [
    "CourseEvidenceRecord",
    "CourseLearningAdapter",
    "CourseObjectiveRecord",
    "CoursePolicy",
    "CourseReviewRecord",
    "CourseState",
]
