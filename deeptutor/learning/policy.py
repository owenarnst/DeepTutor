"""Compatibility façade for the Mastery learning policy.

No LLM calls, no I/O. This is the engine the chat-loop tutor consults each
turn. It answers three questions:

* **is this objective mastered?** (:func:`is_mastered` — a HARD, per-type gate)
* **what should the learner work on next?** (:func:`next_objective`)
* **what does the whole map look like?** (:func:`map_summary`)

The gate is the heart of mastery-based learning. An objective only counts as
mastered when the evidence clears its threshold, and :func:`next_objective`
keeps returning the same objective until it does — advancement is *computed
from what is mastered*, never tracked by a stage counter. Objective ordering
follows module order then knowledge-point order; an objective the learner has
already proven is skipped (the "test out" / compression path) because the gate
reads proven mastery, not a fixed sequence of stages.
"""

from __future__ import annotations

from dataclasses import dataclass

from deeptutor.learning.mastery_adapter import (
    QUALITATIVE_TYPES,
    QUANTITATIVE_GATE,
    MasteryLearningAdapter,
    get_mastery_adapter,
)
from deeptutor.learning.models import (
    KnowledgePoint,
    KnowledgeType,
    LearningProgress,
    ReviewTask,
)

# Historical private constant retained as a read-only compatibility alias;
# ownership of the value remains in the Mastery adapter.
_QUALITATIVE_PASS_DISPLAY = MasteryLearningAdapter.gate_threshold(KnowledgeType.CONCEPT)


def gate_threshold(kp_type: KnowledgeType) -> float:
    """The quantitative mastery bar for *kp_type* (qualitative types report
    their pass-display value so callers have a single number to show)."""
    return MasteryLearningAdapter.gate_threshold(kp_type)


def is_mastered(progress: LearningProgress, kp: KnowledgePoint) -> bool:
    """Whether ``kp`` clears its mastery gate.

    * MEMORY / PROCEDURE: recency-weighted accuracy ≥ the type's threshold.
    * CONCEPT / DESIGN: a recorded qualitative pass (``mastery_assess``).
    """
    return get_mastery_adapter().is_mastered(progress, kp)


def display_mastery(progress: LearningProgress, kp: KnowledgePoint) -> float:
    """A 0..1 number for the map UI. Qualitatively-mastered points show full;
    otherwise the recency-weighted accuracy stands in."""
    return get_mastery_adapter().display_mastery(progress, kp)


def objective_status(progress: LearningProgress, kp: KnowledgePoint) -> str:
    """``"mastered"`` | ``"learning"`` | ``"new"`` for one knowledge point."""
    return get_mastery_adapter().objective_status(progress, kp)


def due_reviews(progress: LearningProgress, *, now: float | None = None) -> list[ReviewTask]:
    """Spaced-repetition tasks whose ``due_at`` has passed, highest priority
    first. Pure read over ``progress.review_queue`` (built by the scheduler)."""
    adapter = get_mastery_adapter(progress)
    return [adapter.to_product_task(item) for item in adapter.due_reviews(now)]


@dataclass(frozen=True)
class NextStep:
    """What the tutor should do next, decided by the gate — not a stage cursor.

    ``action`` is advisory for the model's pedagogy; the binding fact is the
    objective and whether it is mastered. Values:

    * ``answer_pending`` — a posed question awaits the learner's answer.
    * ``review`` — a spaced-repetition item is due.
    * ``probe`` — an untouched objective; test out before teaching.
    * ``practice`` — a quantitative objective below its gate.
    * ``assess`` — a qualitative objective awaiting a Feynman-style check.
    * ``complete`` — every objective mastered, nothing due.
    """

    action: str
    module_id: str = ""
    module_name: str = ""
    knowledge_point_id: str = ""
    knowledge_point_name: str = ""
    knowledge_point_type: str = ""
    status: str = ""
    gate: str = ""
    mastery: float = 0.0
    threshold: float = 0.0
    reason: str = ""
    pending_prompt: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "module_id": self.module_id,
            "module_name": self.module_name,
            "knowledge_point_id": self.knowledge_point_id,
            "knowledge_point_name": self.knowledge_point_name,
            "knowledge_point_type": self.knowledge_point_type,
            "status": self.status,
            "gate": self.gate,
            "mastery": round(self.mastery, 3),
            "threshold": round(self.threshold, 3),
            "reason": self.reason,
            "pending_prompt": self.pending_prompt,
        }


def find_knowledge_point(
    progress: LearningProgress, kp_id: str
) -> tuple[KnowledgePoint | None, str, str]:
    """Return ``(kp, module_id, module_name)`` for *kp_id*, or ``(None, "", "")``."""
    return get_mastery_adapter().find_knowledge_point(progress, kp_id)


def _gate_kind(kp: KnowledgePoint) -> str:
    return get_mastery_adapter()._gate_kind(kp)


def next_objective(progress: LearningProgress, *, now: float | None = None) -> NextStep:
    """Decide the next thing to work on. Order of precedence:

    1. an outstanding posed question (grade it before moving on);
    2. a due spaced-repetition review (don't let mastered ground decay);
    3. the first not-yet-mastered objective in module/KP order (the gate IS
       the cursor — mastered objectives are skipped);
    4. otherwise the path is complete.
    """
    decision = get_mastery_adapter(progress).next_action(now)
    kp, _module_id, _module_name = find_knowledge_point(progress, decision.objective_id)
    return NextStep(
        action=decision.action,
        module_id=decision.container_id,
        module_name=decision.container_name,
        knowledge_point_id=decision.objective_id,
        knowledge_point_name=decision.objective_name,
        knowledge_point_type=decision.objective_category,
        status=decision.status,
        gate=_gate_kind(kp) if kp else "",
        mastery=decision.score,
        threshold=decision.target,
        reason=decision.reason,
        pending_prompt=decision.pending_prompt,
    )


def map_summary(progress: LearningProgress, *, now: float | None = None) -> dict:
    """A compact, render-ready snapshot of the whole path for the tutor's
    ``mastery_status`` tool and the dashboard."""
    return get_mastery_adapter(progress).map_summary(now=now)


__all__ = [
    "QUANTITATIVE_GATE",
    "QUALITATIVE_TYPES",
    "NextStep",
    "gate_threshold",
    "is_mastered",
    "display_mastery",
    "objective_status",
    "due_reviews",
    "find_knowledge_point",
    "next_objective",
    "map_summary",
]
