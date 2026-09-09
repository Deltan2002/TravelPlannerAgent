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
from app.tools import PlanReadinessTool
from app.workflow.state import TravelState


class TravelWorkflow:
    def __init__(
        self,
        settings: Settings,
        research_agent: ResearchAgent,
        itinerary_agent: ItineraryPlannerAgent,
        readiness_tool: PlanReadinessTool,
    ) -> None:
        self.settings = settings
        self.research_agent = research_agent
        self.itinerary_agent = itinerary_agent
        self.readiness_tool = readiness_tool

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
        draft, stretch = self.itinerary_agent.run(
            request,
            research,
            feedback=state.get("review_feedback"),
            modifications=state.get("modifications"),
        )
        draft_readiness = (
            self.readiness_tool.evaluate(request, research, draft) if draft else None
        )
        stretch_readiness = (
            self.readiness_tool.evaluate(request, research, stretch) if stretch else None
        )
        awaiting = {
            "kind": "itinerary_review",
            "message": (
                "Review each scenario and its readiness checks, then approve, reject, "
                "or modify the plan."
            ),
            "allowed_actions": ["approve", "reject", "modify"],
            "available_plan_choices": [
                choice
                for choice, plan, readiness in (
                    ("within_budget", draft, draft_readiness),
                    ("stretch", stretch, stretch_readiness),
                )
                if plan is not None and readiness is not None and readiness.status != "blocked"
            ],
            "readiness": {
                "within_budget": (
                    draft_readiness.model_dump(mode="json") if draft_readiness else None
                ),
                "stretch": (
                    stretch_readiness.model_dump(mode="json") if stretch_readiness else None
                ),
            },
            "review_endpoint": f"/plan/{state['plan_id']}/review",
        }
        return {
            "draft_plan": draft.model_dump(mode="json") if draft else None,
            "draft_readiness": (
                draft_readiness.model_dump(mode="json") if draft_readiness else None
            ),
            "stretch_plan": stretch.model_dump(mode="json") if stretch else None,
            "stretch_readiness": (
                stretch_readiness.model_dump(mode="json") if stretch_readiness else None
            ),
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
            plan_choice=(
                (review.plan_choice or "within_budget")
                if review.action == ReviewAction.APPROVE
                else None
            ),
            submitted_at=datetime.now(UTC),
        )
        history = [*state.get("review_history", []), record.model_dump(mode="json")]
        update: TravelState = {
            "review_history": history,
            "awaiting_input": None,
        }
        if review.action == ReviewAction.APPROVE:
            update.update(
                {
                    "review_feedback": review.feedback,
                    "modifications": None,
                    "selected_plan": review.plan_choice or "within_budget",
                }
            )
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
                "selected_plan": None,
            }
        )
        if review.action == ReviewAction.REJECT:
            return Command(update=update, goto="research_agent")
        return Command(update=update, goto="itinerary_planner_agent")

    @staticmethod
    def finalize(state: TravelState) -> TravelState:
        selected_plan = state.get("selected_plan") or "within_budget"
        plan_key = "draft_plan" if selected_plan == "within_budget" else "stretch_plan"
        approved_plan = state.get(plan_key)
        if approved_plan is None:
            raise ValueError(f"the {selected_plan} plan is not available")
        final_values = {
            **approved_plan,
            "plan_id": state["plan_id"],
            "finalized_at": datetime.now(UTC),
            "approval_note": (
                f"The {selected_plan} plan was approved through the human review endpoint."
            ),
        }
        final_plan = FinalPlan.model_validate(final_values)
        return {
            "final_plan": final_plan.model_dump(mode="json"),
            "status": PlanStatus.FINALIZED.value,
            "awaiting_input": None,
            "review_feedback": None,
            "modifications": None,
            "selected_plan": selected_plan,
            "error": None,
        }
