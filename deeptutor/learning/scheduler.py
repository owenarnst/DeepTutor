"""Compatibility façade for Mastery's spaced-repetition scheduler.

The public class and values stay at their historical import path, while the
decision work is routed through :class:`MasteryLearningAdapter`.  The neutral
kernel never owns these intervals or priorities.
"""

from __future__ import annotations

from deeptutor.learning.mastery_adapter import (
    INTERVAL_SEQUENCES,
    MasteryLearningAdapter,
)
from deeptutor.learning.models import (
    KnowledgeType,
    LearningProgress,
    RepetitionState,
    ReviewTask,
)


class SpacedRepetitionScheduler:
    """Legacy scheduler interface backed by the Mastery adapter."""

    def __init__(self) -> None:
        self._adapter = MasteryLearningAdapter()
        # Existing callers inspect this attribute in debugging contexts.
        self.DEBUG_MODE = self._adapter.debug_mode

    def get_initial_state(self, knowledge_type: KnowledgeType) -> RepetitionState:
        schedule = self._adapter.initial_review_schedule(knowledge_type)
        return self._adapter.to_product_state(schedule)

    def schedule_next(
        self, state: RepetitionState, knowledge_type: KnowledgeType, is_correct: bool
    ) -> RepetitionState:
        schedule = self._adapter.schedule_review(
            self._adapter.to_review_schedule(state),
            knowledge_type,
            is_correct,
        )
        # Preserve the historical in-place mutation contract: callers may keep
        # references to ``state`` while receiving the same object back.
        state.interval_index = schedule.interval_index
        state.consecutive_correct = schedule.consecutive_correct
        state.consecutive_wrong = schedule.consecutive_wrong
        state.next_review_at = schedule.due_at
        return state

    def get_due_tasks(self, progress: LearningProgress, max_tasks: int = 5) -> list[ReviewTask]:
        return self._adapter.get_due_tasks(progress, max_tasks=max_tasks)

    def build_review_queue(self, progress: LearningProgress) -> list[ReviewTask]:
        return self._adapter.build_review_queue(progress)

    def _seconds_per_unit(self) -> float:
        return self._adapter._seconds_per_unit()


__all__ = ["SpacedRepetitionScheduler", "INTERVAL_SEQUENCES"]
