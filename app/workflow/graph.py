from datetime import UTC, datetime
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.itinerary_agent import ItineraryPlannerAgent
from app.agents.research_agent import ResearchAgent
from app.config import Settings
from app.models import (
    FinalPlan,
    PlanStatus,
    ReviewAction,
    ReviewRecord,
    ReviewRequest,
    TravelRequest,
)
from app.workflow.state import TravelState


class TravelWorkflow:
    def __init__(
        self,
        settings: Settings,
        research_agent: ResearchAgent,
        itinerary_agent: ItineraryPlannerAgent,
    ) -> None:
        self.settings = settings
        self.research_agent = research_agent
        self.itinerary_agent = itinerary_agent

    def compile(self, checkpointer):
        builder = StateGraph(TravelState)
        builder.add_node("validate_request", self.validate_request)
        builder.add_node("research_agent", self.run_research_agent)
        builder.add_node("itinerary_planner_agent", self.run_itinerary_agent)
        builder.add_node("human_review", self.human_review)
        builder.add_node("finalize", self.finalize)

        builder.add_edge(START, "validate_request")
        builder.add_edge("validate_request", "research_agent")
        builder.add_edge("research_agent", "itinerary_planner_agent")
        builder.add_edge("itinerary_planner_agent", "human_review")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=checkpointer)

    @staticmethod
    def validate_request(state: TravelState) -> TravelState:
        request = TravelRequest.model_validate(state["request"])
        return {
            "request": request.model_dump(mode="json"),
            "status": PlanStatus.RESEARCHING.value,
            "error": None,
        }

    def run_research_agent(self, state: TravelState) -> TravelState:
        request = TravelRequest.model_validate(state["request"])
        report = self.research_agent.run(request, feedback=state.get("review_feedback"))
        return {
            "research": report.model_dump(mode="json"),
            "status": PlanStatus.PLANNING.value,
        }

    def run_itinerary_agent(self, state: TravelState) -> TravelState:
        from app.models import ResearchReport

        request = TravelRequest.model_validate(state["request"])
        research = ResearchReport.model_validate(state["research"])
        draft = self.itinerary_agent.run(
            request,
            research,
            feedback=state.get("review_feedback"),
            modifications=state.get("modifications"),
        )
        awaiting = {
            "kind": "itinerary_review",
            "message": "Review the draft and approve, reject with feedback, or modify it.",
            "allowed_actions": ["approve", "reject", "modify"],
            "review_endpoint": f"/plan/{state['plan_id']}/review",
        }
        return {
            "draft_plan": draft.model_dump(mode="json"),
            "status": PlanStatus.AWAITING_REVIEW.value,
            "awaiting_input": awaiting,
            "error": None,
        }

    def human_review(
        self, state: TravelState
    ) -> Command[Literal["research_agent", "itinerary_planner_agent", "finalize"]]:
        decision_payload = interrupt(state["awaiting_input"])
        review = ReviewRequest.model_validate(decision_payload)
        record = ReviewRecord(
            action=review.action,
            feedback=review.feedback,
            modifications=(
                review.modifications.model_dump(mode="json") if review.modifications else None
            ),
            submitted_at=datetime.now(UTC),
        )
        history = [*state.get("review_history", []), record.model_dump(mode="json")]
        update: TravelState = {
            "review_history": history,
            "awaiting_input": None,
        }
        if review.action == ReviewAction.APPROVE:
            update.update({"review_feedback": review.feedback, "modifications": None})
            return Command(update=update, goto="finalize")

        next_revision = state.get("revision_count", 0) + 1
        if next_revision > self.settings.max_revisions:
            raise ValueError(f"maximum revision count ({self.settings.max_revisions}) exceeded")
        update.update(
            {
                "status": PlanStatus.REVISING.value,
                "revision_count": next_revision,
                "review_feedback": review.feedback,
                "modifications": (
                    review.modifications.model_dump(mode="json")
                    if review.modifications
                    else None
                ),
            }
        )
        if review.action == ReviewAction.REJECT:
            return Command(update=update, goto="research_agent")
        return Command(update=update, goto="itinerary_planner_agent")

    @staticmethod
    def finalize(state: TravelState) -> TravelState:
        final_values = {
            **state["draft_plan"],
            "plan_id": state["plan_id"],
            "finalized_at": datetime.now(UTC),
            "approval_note": "Approved through the human-in-the-loop review endpoint.",
        }
        final_plan = FinalPlan.model_validate(final_values)
        return {
            "final_plan": final_plan.model_dump(mode="json"),
            "status": PlanStatus.FINALIZED.value,
            "awaiting_input": None,
            "review_feedback": None,
            "modifications": None,
            "error": None,
        }
