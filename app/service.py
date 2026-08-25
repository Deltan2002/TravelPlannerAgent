import sqlite3
import threading
from typing import Any
from uuid import uuid4

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.agents import ItineraryPlannerAgent, ResearchAgent
from app.config import Settings
from app.llm import StructuredLLM
from app.models import (
    FinalPlan,
    PlanAccepted,
    PlanResponse,
    PlanStatus,
    ReviewAction,
    ReviewRequest,
    TravelRequest,
)
from app.tools import (
    BudgetAllocatorTool,
    DestinationContextTool,
    PackingListTool,
    WebSearchTool,
)
from app.workflow import TravelWorkflow


class TravelPlanService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        self._connection = sqlite3.connect(
            str(settings.database_path), check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._checkpointer = SqliteSaver(self._connection)
        self._lock = threading.RLock()

        http_client = httpx.Client(timeout=settings.http_timeout_seconds)
        self._http_client = http_client
        llm = StructuredLLM(settings, http_client)
        research_agent = ResearchAgent(
            WebSearchTool(settings, http_client),
            DestinationContextTool(settings, http_client),
            llm,
        )
        itinerary_agent = ItineraryPlannerAgent(
            BudgetAllocatorTool(), PackingListTool(), llm
        )
        self.graph = TravelWorkflow(settings, research_agent, itinerary_agent).compile(
            self._checkpointer
        )

    @staticmethod
    def _config(plan_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": plan_id}}

    def create_plan(self, request: TravelRequest) -> PlanAccepted:
        plan_id = str(uuid4())
        initial: dict[str, Any] = {
            "plan_id": plan_id,
            "status": PlanStatus.VALIDATING.value,
            "request": request.model_dump(mode="json"),
            "research": None,
            "draft_plan": None,
            "final_plan": None,
            "review_history": [],
            "revision_count": 0,
            "review_feedback": None,
            "modifications": None,
            "awaiting_input": None,
            "error": None,
        }
        try:
            with self._lock:
                self.graph.invoke(initial, config=self._config(plan_id))
        except Exception as exc:
            self._mark_failed(plan_id, exc)
            raise RuntimeError(str(exc)) from exc
        return PlanAccepted(
            plan_id=plan_id,
            status=PlanStatus.AWAITING_REVIEW,
            message="Draft created. Submit a human review decision to continue.",
            status_url=f"/plan/{plan_id}",
            review_url=f"/plan/{plan_id}/review",
        )

    def get_plan(self, plan_id: str) -> PlanResponse:
        values = self._get_values(plan_id)
        public_values = {
            field_name: values[field_name]
            for field_name in PlanResponse.model_fields
            if field_name in values
        }
        return PlanResponse.model_validate(public_values)

    def review_plan(self, plan_id: str, review: ReviewRequest) -> PlanResponse:
        current = self._get_values(plan_id)
        if current["status"] != PlanStatus.AWAITING_REVIEW.value:
            raise ValueError(
                f"plan is '{current['status']}', not awaiting_review"
            )
        if (
            review.action != ReviewAction.APPROVE
            and current.get("revision_count", 0) >= self.settings.max_revisions
        ):
            raise ValueError(
                f"maximum revision count ({self.settings.max_revisions}) reached; approve the plan"
            )
        try:
            with self._lock:
                self.graph.invoke(
                    Command(resume=review.model_dump(mode="json")),
                    config=self._config(plan_id),
                )
        except Exception as exc:
            self._mark_failed(plan_id, exc)
            raise RuntimeError(str(exc)) from exc
        return self.get_plan(plan_id)

    def get_final_plan(self, plan_id: str) -> FinalPlan:
        values = self._get_values(plan_id)
        if values["status"] != PlanStatus.FINALIZED.value or not values.get("final_plan"):
            raise ValueError("final plan is only available after approval")
        return FinalPlan.model_validate(values["final_plan"])

    def _get_values(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self.graph.get_state(self._config(plan_id))
        values = dict(snapshot.values) if snapshot and snapshot.values else {}
        if not values or values.get("plan_id") != plan_id:
            raise LookupError(f"plan '{plan_id}' was not found")
        return values

    def _mark_failed(self, plan_id: str, exc: Exception) -> None:
        try:
            with self._lock:
                self.graph.update_state(
                    self._config(plan_id),
                    {"status": PlanStatus.FAILED.value, "error": str(exc)},
                )
        except Exception:
            pass

    def close(self) -> None:
        self._http_client.close()
        self._connection.close()
