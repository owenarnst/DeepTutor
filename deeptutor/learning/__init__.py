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

from __future__ import annotations

from importlib import import_module

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
    """Lazily expose historical product exports after a neutral import."""

    if name in {"MasteryAdapter", "MasteryLearningAdapter"}:
        return getattr(import_module("deeptutor.learning.mastery_adapter"), name)
    if name in {
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
    }:
        return getattr(import_module("deeptutor.learning.models"), name)
    raise AttributeError(name)
