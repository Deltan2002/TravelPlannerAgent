import logging
import sqlite3
import threading
from typing import Any
from uuid import uuid4

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from redis import Redis

from app.agents import ItineraryPlannerAgent, ResearchAgent
from app.cache import RedisCache
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
    CurrencyConverterTool,
    DestinationContextTool,
    PackingListTool,
    PlanReadinessTool,
    WebSearchTool,
)
from app.workflow import TravelWorkflow

logger = logging.getLogger(__name__)


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
        self._redis_client = (
            Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            if settings.redis_url and settings.cache_ttl_seconds > 0
            else None
        )

        def cache(namespace: str) -> RedisCache:
            return RedisCache(
                settings.redis_url,
                settings.cache_ttl_seconds,
                namespace,
                self._redis_client,
            )

        llm = StructuredLLM(
            settings,
            http_client,
            cache("llm"),
        )
        research_agent = ResearchAgent(
            WebSearchTool(
                settings,
                http_client,
                cache("search"),
                CurrencyConverterTool(
                    settings,
                    http_client,
                    cache("currency"),
                ),
            ),
            DestinationContextTool(
                settings,
                http_client,
                cache("weather"),
            ),
            llm,
        )
        itinerary_agent = ItineraryPlannerAgent(
            BudgetAllocatorTool(), PackingListTool(), llm
        )
        self.graph = TravelWorkflow(
            settings,
            research_agent,
            itinerary_agent,
            PlanReadinessTool(),
        ).compile(self._checkpointer)

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
            "draft_readiness": None,
            "stretch_plan": None,
            "stretch_readiness": None,
            "final_plan": None,
            "review_history": [],
            "revision_count": 0,
            "review_feedback": None,
            "modifications": None,
            "selected_plan": None,
            "awaiting_input": None,
            "error": None,
        }
        try:
            with self._lock:
                self.graph.invoke(initial, config=self._config(plan_id))
        except Exception as exc:
            logger.exception("Plan creation failed")
            self._mark_failed(plan_id, exc)
            raise RuntimeError(str(exc)) from exc
        return PlanAccepted(
            plan_id=plan_id,
            status=PlanStatus.AWAITING_REVIEW,
            message=(
                "Planning and readiness checks completed. Review the available budget "
                "scenarios to continue."
            ),
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
        with self._lock:
            current = self._get_values(plan_id)
            if current["status"] != PlanStatus.AWAITING_REVIEW.value:
                raise ValueError(f"plan is '{current['status']}', not awaiting_review")
            if (
                review.action != ReviewAction.APPROVE
                and current.get("revision_count", 0) >= self.settings.max_revisions
            ):
                raise ValueError(
                    f"maximum revision count ({self.settings.max_revisions}) reached; "
                    "approve the plan"
                )
            if review.action == ReviewAction.APPROVE:
                choice = review.plan_choice or "within_budget"
                plan_key = "draft_plan" if choice == "within_budget" else "stretch_plan"
                if not current.get(plan_key):
                    raise ValueError(f"the {choice} plan is not available")
                readiness_key = (
                    "draft_readiness" if choice == "within_budget" else "stretch_readiness"
                )
                readiness = current.get(readiness_key)
                if readiness and readiness.get("status") == "blocked":
                    raise ValueError(
                        f"the {choice} plan has unresolved readiness blockers"
                    )
            if review.modifications:
                request = TravelRequest.model_validate(current["request"])
                invalid_days = sorted(
                    change.day
                    for change in review.modifications.day_changes
                    if change.day > request.days
                )
                if invalid_days:
                    invalid = ", ".join(str(day) for day in invalid_days)
                    raise ValueError(
                        f"modification day(s) {invalid} outside trip range 1-{request.days}"
                    )
            try:
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
        if self._redis_client is not None:
            self._redis_client.close()
        self._connection.close()
