"""Mastery Path — structured mastery-based learning engine.

Modules:
    models      — Pydantic data models
    storage     — JSON persistence
    scheduler   — Spaced repetition
    mastery     — Mastery scoring policy (swappable)
    grading     — Deterministic answer grading
    service     — Business logic
    prompts     — LLM prompt templates
"""

from deeptutor.learning.kernel import (
    Evidence,
    LearningKernel,
    NextActionDecision,
    Objective,
    ObjectiveIdentity,
    ObjectiveState,
    ProgressionDecision,
    ReviewDueState,
    ReviewItem,
    ReviewSchedule,
)
from deeptutor.learning.models import (
    DiagnosticResult,
    ErrorRecord,
    ErrorType,
    KnowledgePoint,
    KnowledgeType,
    LearningModule,
    LearningProgress,
    LearningStage,
    QuizAttempt,
    RepetitionState,
    RetryAttempt,
    ReviewTask,
)

__all__ = [
    "DiagnosticResult",
    "ErrorRecord",
    "ErrorType",
    "KnowledgePoint",
    "KnowledgeType",
    "LearningModule",
    "LearningProgress",
    "LearningStage",
    "QuizAttempt",
    "RepetitionState",
    "RetryAttempt",
    "ReviewTask",
    "Evidence",
    "LearningKernel",
    "NextActionDecision",
    "Objective",
    "ObjectiveIdentity",
    "ObjectiveState",
    "ProgressionDecision",
    "ReviewDueState",
    "ReviewItem",
    "ReviewSchedule",
    "MasteryAdapter",
    "MasteryLearningAdapter",
]


def __getattr__(name: str):
    """Lazily expose the product adapter without polluting the kernel import."""

    if name in {"MasteryAdapter", "MasteryLearningAdapter"}:
        from deeptutor.learning.mastery_adapter import (
            MasteryAdapter,
            MasteryLearningAdapter,
        )

        adapters = {
            "MasteryAdapter": MasteryAdapter,
            "MasteryLearningAdapter": MasteryLearningAdapter,
        }
        return adapters[name]
    raise AttributeError(name)
