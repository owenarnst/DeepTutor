"""Mastery product adapter for the neutral learning-kernel contract.

The adapter is the only production bridge from the Mastery gradebook and its
pedagogy to :mod:`deeptutor.learning.kernel`.  Existing ``policy`` and
``scheduler`` modules remain public compatibility façades and delegate here;
their legacy values and serialized models are intentionally unchanged.
"""

from __future__ import annotations

import os
import time

from deeptutor.learning.grading import classify_error as _classify_error
from deeptutor.learning.grading import grade_answer as _grade_answer
from deeptutor.learning.kernel import (
    Evidence,
    NextActionDecision,
    ObjectiveIdentity,
    ObjectiveState,
    ProgressionDecision,
    ReviewItem,
    ReviewSchedule,
)
from deeptutor.learning.mastery import compute_mastery as _compute_mastery
from deeptutor.learning.models import (
    KnowledgePoint,
    KnowledgeType,
    LearningProgress,
    RepetitionState,
    ReviewTask,
)

# Mastery policy values belong to this product adapter, not to the neutral
# kernel.  Existing modules re-export the same objects for import compatibility.
QUANTITATIVE_GATE: dict[KnowledgeType, float] = {
    KnowledgeType.MEMORY: 0.9,
    KnowledgeType.PROCEDURE: 0.9,
}
QUALITATIVE_TYPES: frozenset[KnowledgeType] = frozenset(
    {KnowledgeType.CONCEPT, KnowledgeType.DESIGN}
)
_QUALITATIVE_PASS_DISPLAY = 1.0


# Spaced-repetition policy is also Mastery-owned.  ``scheduler.py`` preserves
# its historical import path by re-exporting these constants.
INTERVAL_SEQUENCES: dict[KnowledgeType, list[int]] = {
    KnowledgeType.MEMORY: [0, 1, 3, 7, 14, 30, 60],
    KnowledgeType.CONCEPT: [3, 7, 14, 30],
    KnowledgeType.PROCEDURE: [3, 7, 14],
    KnowledgeType.DESIGN: [14, 28],
}

_TYPE_PRIORITY: dict[KnowledgeType, int] = {
    KnowledgeType.MEMORY: 2,
    KnowledgeType.CONCEPT: 3,
    KnowledgeType.PROCEDURE: 4,
    KnowledgeType.DESIGN: 5,
}


class MasteryLearningAdapter:
    """Translate Mastery state and policy into neutral kernel values.

    ``progress`` is optional so the same adapter can be used as the stateless
    product-policy façade by ``LearningService`` and ``SpacedRepetitionScheduler``.
    Methods that expose neutral objective decisions require a bound progress
    object; product-specific policy helpers accept one explicitly for backward
    compatibility with the existing call sites.
    """

    def __init__(self, progress: LearningProgress | None = None) -> None:
        self.progress = progress
        self.debug_mode = os.environ.get("LEARNING_DEBUG", "").lower() in (
            "1",
            "true",
            "yes",
        )

    # ── neutral objective/evidence decisions ─────────────────────────────

    def objective_states(self) -> tuple[ObjectiveState, ...]:
        progress = self._require_progress()
        states: list[ObjectiveState] = []
        for module in sorted(progress.modules, key=lambda item: item.order):
            for position, kp in enumerate(module.knowledge_points):
                states.append(
                    ObjectiveState(
                        identity=ObjectiveIdentity(
                            objective_id=kp.id,
                            name=kp.name,
                            category=kp.type.value,
                            container_id=module.id,
                            position=position,
                        ),
                        status=self.objective_status(progress, kp),
                        score=self.display_mastery(progress, kp),
                        target=self.gate_threshold(kp.type),
                    )
                )
        return tuple(states)

    def evidence_for(self, objective_id: str) -> tuple[Evidence, ...]:
        progress = self._require_progress()
        evidence = [
            Evidence(
                objective_id=attempt.knowledge_point_id,
                kind="assessment",
                outcome=attempt.is_correct,
                observed_at=attempt.timestamp,
                source=attempt.question_id,
                metadata={"module_id": attempt.module_id},
            )
            for attempt in progress.quiz_attempts
            if attempt.knowledge_point_id == objective_id
        ]
        if objective_id in progress.qualitative_mastery:
            evidence.append(
                Evidence(
                    objective_id=objective_id,
                    kind="judgement",
                    outcome=progress.qualitative_mastery[objective_id],
                    source="qualitative-assessment",
                )
            )
        evidence.sort(key=lambda item: item.observed_at)
        return tuple(evidence)

    def progression_decision(self, objective_id: str) -> ProgressionDecision:
        progress = self._require_progress()
        kp, _module_id, _module_name = self.find_knowledge_point(progress, objective_id)
        if kp is None:
            return ProgressionDecision(
                decision="continue",
                objective_id=objective_id,
                reason="The objective is not present in the active path.",
                status="unknown",
            )
        status = self.objective_status(progress, kp)
        if self.is_mastered(progress, kp):
            return ProgressionDecision(
                decision="advance",
                objective_id=objective_id,
                reason="The objective cleared its Mastery gate.",
                status=status,
            )
        return ProgressionDecision(
            decision="continue",
            objective_id=objective_id,
            reason="The objective remains below its Mastery gate.",
            status=status,
        )

    def due_reviews(self, now: float | None = None) -> tuple[ReviewItem, ...]:
        progress = self._require_progress()
        moment = time.time() if now is None else now
        due: list[ReviewItem] = []
        for task in progress.review_queue:
            if task.due_at > moment:
                continue
            due.append(self._review_item(task, is_due=True))
        due.sort(key=lambda item: item.priority)
        return tuple(due)

    def next_action(self, now: float | None = None) -> NextActionDecision:
        progress = self._require_progress()
        moment = time.time() if now is None else now

        pending = progress.pending_question
        if pending is not None:
            kp, module_id, module_name = self.find_knowledge_point(
                progress, pending.knowledge_point_id
            )
            return NextActionDecision(
                action="answer_pending",
                objective_id=pending.knowledge_point_id,
                objective_name=kp.name if kp else "",
                objective_category=kp.type.value if kp else "",
                container_id=module_id or pending.module_id,
                status=self.objective_status(progress, kp) if kp else "learning",
                container_name=module_name,
                score=self.display_mastery(progress, kp) if kp else 0.0,
                target=self.gate_threshold(kp.type) if kp else 0.0,
                reason=(
                    "A posed question is awaiting the learner's answer; "
                    "grade it with mastery_grade."
                ),
                pending_prompt=pending.prompt,
            )

        due = self.due_reviews(moment)
        if due:
            task = due[0]
            kp, module_id, module_name = self.find_knowledge_point(progress, task.objective_id)
            if kp is not None:
                return NextActionDecision(
                    action="review",
                    objective_id=kp.id,
                    objective_name=kp.name,
                    objective_category=kp.type.value,
                    container_id=module_id,
                    container_name=module_name,
                    status=self.objective_status(progress, kp),
                    score=self.display_mastery(progress, kp),
                    target=self.gate_threshold(kp.type),
                    reason="This objective is due for spaced-repetition review.",
                )

        for module in sorted(progress.modules, key=lambda item: item.order):
            for kp in module.knowledge_points:
                if self.is_mastered(progress, kp):
                    continue
                status = self.objective_status(progress, kp)
                gate = self._gate_kind(kp)
                action = (
                    "probe"
                    if status == "new"
                    else ("assess" if gate == "qualitative" else "practice")
                )
                reason = (
                    "Untouched objective — probe first to let the learner test out."
                    if status == "new"
                    else "Objective is below its mastery gate; keep working it until it clears."
                )
                return NextActionDecision(
                    action=action,
                    objective_id=kp.id,
                    objective_name=kp.name,
                    objective_category=kp.type.value,
                    container_id=module.id,
                    container_name=module.name,
                    status=status,
                    score=self.display_mastery(progress, kp),
                    target=self.gate_threshold(kp.type),
                    reason=reason,
                )

        return NextActionDecision(
            action="complete",
            reason="All objectives are mastered and no reviews are due.",
        )

    # ── Mastery-owned policy values ──────────────────────────────────────

    @staticmethod
    def gate_threshold(kp_type: KnowledgeType) -> float:
        if kp_type in QUALITATIVE_TYPES:
            return _QUALITATIVE_PASS_DISPLAY
        return QUANTITATIVE_GATE.get(kp_type, 0.9)

    @staticmethod
    def is_mastered(progress: LearningProgress, kp: KnowledgePoint) -> bool:
        if kp.type in QUALITATIVE_TYPES:
            return bool(progress.qualitative_mastery.get(kp.id, False))
        return progress.mastery_levels.get(kp.id, 0.0) >= MasteryLearningAdapter.gate_threshold(
            kp.type
        )

    @staticmethod
    def display_mastery(progress: LearningProgress, kp: KnowledgePoint) -> float:
        if kp.type in QUALITATIVE_TYPES and progress.qualitative_mastery.get(kp.id):
            return _QUALITATIVE_PASS_DISPLAY
        return float(progress.mastery_levels.get(kp.id, 0.0))

    @staticmethod
    def objective_status(progress: LearningProgress, kp: KnowledgePoint) -> str:
        if MasteryLearningAdapter.is_mastered(progress, kp):
            return "mastered"
        seen = any(a.knowledge_point_id == kp.id for a in progress.quiz_attempts) or (
            kp.id in progress.qualitative_mastery
        )
        return "learning" if seen else "new"

    @staticmethod
    def find_knowledge_point(
        progress: LearningProgress, kp_id: str
    ) -> tuple[KnowledgePoint | None, str, str]:
        for module in progress.modules:
            for kp in module.knowledge_points:
                if kp.id == kp_id:
                    return kp, module.id, module.name
        return None, "", ""

    @staticmethod
    def _gate_kind(kp: KnowledgePoint) -> str:
        return "qualitative" if kp.type in QUALITATIVE_TYPES else "quantitative"

    def map_summary(
        self, progress: LearningProgress | None = None, *, now: float | None = None
    ) -> dict:
        progress = progress or self._require_progress()
        counts = {"mastered": 0, "learning": 0, "new": 0, "total": 0}
        modules_out: list[dict] = []
        for module in sorted(progress.modules, key=lambda item: item.order):
            kps_out: list[dict] = []
            mastered = 0
            for kp in module.knowledge_points:
                status = self.objective_status(progress, kp)
                counts[status] += 1
                counts["total"] += 1
                if status == "mastered":
                    mastered += 1
                kps_out.append(
                    {
                        "id": kp.id,
                        "name": kp.name,
                        "type": kp.type.value,
                        "status": status,
                        "mastery": round(self.display_mastery(progress, kp), 3),
                    }
                )
            modules_out.append(
                {
                    "id": module.id,
                    "name": module.name,
                    "order": module.order,
                    "mastered": mastered,
                    "total": len(module.knowledge_points),
                    "knowledge_points": kps_out,
                }
            )
        bound = self if self.progress is progress else MasteryLearningAdapter(progress)
        return {
            "counts": counts,
            "due_reviews": len(bound.due_reviews(now)),
            "complete": counts["total"] > 0 and counts["mastered"] == counts["total"],
            "modules": modules_out,
        }

    # ── Mastery-owned review scheduling ──────────────────────────────────

    def _seconds_per_unit(self) -> float:
        return 1.0 if self.debug_mode else 86400.0

    def initial_review_schedule(
        self, knowledge_type: KnowledgeType, *, now: float | None = None
    ) -> ReviewSchedule:
        intervals = INTERVAL_SEQUENCES[knowledge_type]
        moment = time.time() if now is None else now
        return ReviewSchedule(
            objective_id="",
            interval_index=0,
            consecutive_correct=0,
            consecutive_wrong=0,
            due_at=moment + intervals[0] * self._seconds_per_unit(),
        )

    def schedule_review(
        self,
        state: ReviewSchedule,
        knowledge_type: KnowledgeType,
        is_correct: bool,
        *,
        now: float | None = None,
    ) -> ReviewSchedule:
        intervals = INTERVAL_SEQUENCES[knowledge_type]
        max_index = len(intervals) - 1
        interval_index = state.interval_index
        consecutive_correct = state.consecutive_correct
        consecutive_wrong = state.consecutive_wrong

        if is_correct:
            consecutive_wrong = 0
            consecutive_correct += 1
            if consecutive_correct >= 2:
                interval_index += 2
                consecutive_correct = 0
            else:
                interval_index += 1
        else:
            consecutive_wrong += 1
            consecutive_correct = 0
            interval_index = max(0, interval_index - 1)
            if consecutive_wrong >= 2:
                consecutive_wrong = 0

        interval_index = max(0, min(interval_index, max_index))
        moment = time.time() if now is None else now
        return ReviewSchedule(
            objective_id=state.objective_id,
            interval_index=interval_index,
            consecutive_correct=consecutive_correct,
            consecutive_wrong=consecutive_wrong,
            due_at=moment + intervals[interval_index] * self._seconds_per_unit(),
        )

    def review_items_for(
        self, progress: LearningProgress, *, now: float | None = None
    ) -> tuple[ReviewItem, ...]:
        moment = time.time() if now is None else now
        error_kps = {
            rec.knowledge_point_id
            for rec in progress.error_records
            if rec.status in ("active", "retrying")
        }
        items: list[ReviewItem] = []
        for kp_id, state in progress.repetition_states.items():
            kp_type = progress.knowledge_types.get(kp_id, KnowledgeType.MEMORY)
            priority = 1 if kp_id in error_kps else _TYPE_PRIORITY[kp_type]
            schedule = ReviewSchedule(
                objective_id=kp_id,
                due_at=state.next_review_at,
                interval_index=state.interval_index,
                consecutive_correct=state.consecutive_correct,
                consecutive_wrong=state.consecutive_wrong,
            )
            items.append(
                ReviewItem(
                    objective_id=kp_id,
                    category=kp_type.value,
                    due_at=state.next_review_at,
                    priority=priority,
                    interval_index=state.interval_index,
                    is_due=state.next_review_at <= moment,
                    item_id=f"review_{kp_id}",
                    schedule=schedule,
                )
            )
        return tuple(items)

    def due_reviews_for(
        self,
        progress: LearningProgress,
        *,
        now: float | None = None,
        max_tasks: int | None = None,
    ) -> tuple[ReviewItem, ...]:
        moment = time.time() if now is None else now
        due = [item for item in self.review_items_for(progress, now=moment) if item.is_due]
        due.sort(key=lambda item: item.priority)
        if max_tasks is not None:
            due = due[:max_tasks]
        return tuple(due)

    @staticmethod
    def to_product_state(
        schedule: ReviewSchedule,
        *,
        existing: RepetitionState | None = None,
    ) -> RepetitionState:
        """Translate a neutral schedule without breaking legacy identity.

        ``LearningProgress.repetition_states`` is a live product-owned map.
        When a compatibility façade already has that state object, returning
        it is part of the historical contract; the neutral schedule remains a
        read-only decision value and does not replace the product object.
        """

        if existing is not None:
            return existing
        return RepetitionState(
            interval_index=schedule.interval_index,
            consecutive_correct=schedule.consecutive_correct,
            consecutive_wrong=schedule.consecutive_wrong,
            next_review_at=schedule.due_at,
        )

    @staticmethod
    def to_review_schedule(state: RepetitionState) -> ReviewSchedule:
        return ReviewSchedule(
            objective_id="",
            due_at=state.next_review_at,
            interval_index=state.interval_index,
            consecutive_correct=state.consecutive_correct,
            consecutive_wrong=state.consecutive_wrong,
        )

    @staticmethod
    def to_product_task(
        item: ReviewItem,
        *,
        existing: ReviewTask | None = None,
        existing_state: RepetitionState | None = None,
    ) -> ReviewTask:
        """Translate a review item while retaining legacy product objects."""

        if existing is not None:
            return existing
        return ReviewTask(
            id=item.item_id or f"review_{item.objective_id}",
            knowledge_point_id=item.objective_id,
            knowledge_type=KnowledgeType(item.category),
            due_at=item.due_at,
            priority=item.priority,
            state=MasteryLearningAdapter.to_product_state(
                item.schedule,
                existing=existing_state,
            ),
        )

    def build_review_queue(self, progress: LearningProgress) -> list[ReviewTask]:
        return [
            self.to_product_task(
                item,
                existing_state=progress.repetition_states.get(item.objective_id),
            )
            for item in self.review_items_for(progress)
        ]

    def get_due_tasks(self, progress: LearningProgress, max_tasks: int = 5) -> list[ReviewTask]:
        moment = time.time()
        due = [
            self._review_item(task, is_due=True)
            for task in progress.review_queue
            if task.due_at <= moment
        ]
        due.sort(key=lambda item: item.priority)
        queued_by_id = {task.id: task for task in progress.review_queue}
        return [
            self.to_product_task(item, existing=queued_by_id.get(item.item_id))
            for item in due[:max_tasks]
        ]

    # ── Product grading/scoring façade ───────────────────────────────────

    @staticmethod
    def compute_mastery(correctness: list[bool]) -> float:
        return _compute_mastery(correctness)

    @staticmethod
    def grade_answer(user_answer: str, expected_answer: str, question_type: str = "short") -> bool:
        return _grade_answer(user_answer, expected_answer, question_type)

    @staticmethod
    def classify_error(user_answer: str):
        return _classify_error(user_answer)

    @staticmethod
    def record_qualitative(
        progress: LearningProgress,
        kp_id: str,
        *,
        passed: bool,
        evidence: str = "",
    ) -> None:
        """Apply the Mastery qualitative gate and its display projection."""

        progress.qualitative_mastery[kp_id] = bool(passed)
        current = progress.mastery_levels.get(kp_id, 0.0)
        progress.mastery_levels[kp_id] = max(current, 1.0) if passed else min(current, 0.4)
        if evidence:
            progress.feynman_explanations[kp_id] = evidence
        progress.updated_at = time.time()

    def _review_item(self, task: ReviewTask, *, is_due: bool) -> ReviewItem:
        state = task.state
        schedule = ReviewSchedule(
            objective_id=task.knowledge_point_id,
            due_at=task.due_at,
            interval_index=state.interval_index,
            consecutive_correct=state.consecutive_correct,
            consecutive_wrong=state.consecutive_wrong,
        )
        return ReviewItem(
            objective_id=task.knowledge_point_id,
            category=task.knowledge_type.value,
            due_at=task.due_at,
            priority=task.priority,
            interval_index=state.interval_index,
            is_due=is_due,
            item_id=task.id,
            schedule=schedule,
        )

    def _require_progress(self) -> LearningProgress:
        if self.progress is None:
            raise ValueError("MasteryLearningAdapter requires a bound progress object")
        return self.progress


MasteryAdapter = MasteryLearningAdapter


def get_mastery_adapter(progress: LearningProgress | None = None) -> MasteryLearningAdapter:
    """Return a product adapter bound to ``progress`` when supplied."""

    return MasteryLearningAdapter(progress)


__all__ = [
    "INTERVAL_SEQUENCES",
    "MasteryAdapter",
    "MasteryLearningAdapter",
    "QUALITATIVE_TYPES",
    "QUANTITATIVE_GATE",
    "get_mastery_adapter",
]
