"""Neutral contracts for learning progress decisions.

This module is deliberately domain-free.  It describes the values exchanged
by a learning engine, but it does not define a gradebook, a scoring threshold,
an interval table, storage format, or a product's pedagogy.  Product adapters
translate their own state and policy into these values at the boundary.

The contracts are small immutable values so a Course implementation, a
Mastery implementation, or a future learning product can be tested against
the same observable shape without sharing product state or policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ObjectiveIdentity:
    """Stable identity and presentation data for one learning objective."""

    objective_id: str
    name: str = ""
    category: str = ""
    container_id: str = ""
    position: int = 0

    @property
    def id(self) -> str:
        """Compatibility-friendly short spelling for the stable identifier."""

        return self.objective_id


# ``Objective`` is a useful neutral shorthand; keep the explicit identity
# name as the canonical public contract.
Objective = ObjectiveIdentity


@dataclass(frozen=True)
class ObjectiveState:
    """Current product-independent state for an objective.

    ``status`` and ``category`` are intentionally opaque strings.  A product
    decides whether they mean ``active``/``complete``, ``learning``/``mastered``
    or another vocabulary; the kernel must not impose a policy on consumers.
    ``score`` and ``target`` are display/decision values, not a gradebook.
    """

    identity: ObjectiveIdentity
    status: str = ""
    score: float = 0.0
    target: float = 0.0

    @property
    def objective_id(self) -> str:
        return self.identity.objective_id


@dataclass(frozen=True)
class Evidence:
    """A neutral observation associated with one objective.

    The kernel does not interpret ``outcome``.  It may be a boolean, a score,
    a label, or another product-owned value; adapters decide how evidence
    affects progression.  Sensitive product data (for example a server-held
    expected answer) must never be placed in this boundary value.
    """

    objective_id: str
    kind: str
    outcome: object = None
    observed_at: float = 0.0
    source: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def timestamp(self) -> float:
        """Alias useful to consumers that call observations timestamps."""

        return self.observed_at


@dataclass(frozen=True)
class ProgressionDecision:
    """A product's decision about continuing or advancing one objective."""

    decision: str
    objective_id: str
    reason: str = ""
    status: str = ""


@dataclass(frozen=True)
class ReviewSchedule:
    """Neutral scheduling state for one objective."""

    objective_id: str
    due_at: float
    interval_index: int = 0
    consecutive_correct: int = 0
    consecutive_wrong: int = 0


@dataclass(frozen=True)
class ReviewItem:
    """A review item and its neutral due-state metadata."""

    objective_id: str
    category: str = ""
    due_at: float = 0.0
    priority: int = 0
    interval_index: int = 0
    is_due: bool = False
    item_id: str = ""
    schedule: ReviewSchedule | None = None

    def __post_init__(self) -> None:
        if self.schedule is None:
            object.__setattr__(
                self,
                "schedule",
                ReviewSchedule(
                    objective_id=self.objective_id,
                    due_at=self.due_at,
                    interval_index=self.interval_index,
                ),
            )


# Explicit alias for callers that name the due-state concept directly.
ReviewDueState = ReviewItem


@dataclass(frozen=True)
class NextActionDecision:
    """The next action selected by a product's learning policy."""

    action: str
    objective_id: str = ""
    reason: str = ""
    status: str = ""
    container_id: str = ""
    container_name: str = ""
    objective_name: str = ""
    objective_category: str = ""
    score: float = 0.0
    target: float = 0.0
    pending_prompt: str = ""


@runtime_checkable
class LearningKernel(Protocol):
    """Observable contract implemented by product learning adapters."""

    def objective_states(self) -> Sequence[ObjectiveState]:
        """Return the current ordered objective states."""

    def evidence_for(self, objective_id: str) -> Sequence[Evidence]:
        """Return evidence associated with one objective."""

    def progression_decision(self, objective_id: str) -> ProgressionDecision:
        """Decide whether an objective should continue or advance."""

    def due_reviews(self, now: float) -> Sequence[ReviewItem]:
        """Return review items due at ``now``."""

    def next_action(self, now: float) -> NextActionDecision:
        """Select the next product action at ``now``."""


__all__ = [
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
]
