from typing import Any, TypedDict


class TravelState(TypedDict, total=False):
    plan_id: str
    status: str
    request: dict[str, Any]
    research: dict[str, Any] | None
    draft_plan: dict[str, Any] | None
    draft_readiness: dict[str, Any] | None
    stretch_plan: dict[str, Any] | None
    stretch_readiness: dict[str, Any] | None
    final_plan: dict[str, Any] | None
    review_history: list[dict[str, Any]]
    revision_count: int
    review_feedback: str | None
    modifications: dict[str, Any] | None
    selected_plan: str | None
    awaiting_input: dict[str, Any] | None
    error: str | None
