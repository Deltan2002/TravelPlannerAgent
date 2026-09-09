import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, status

from app.config import Settings, get_settings
from app.models import (
    FinalPlan,
    HealthResponse,
    PlanAccepted,
    PlanResponse,
    ReviewRequest,
    TravelRequest,
)
from app.service import TravelPlanService

PLAN_EXAMPLES = {
    "connected_destination": {
        "summary": "Connected route to a destination without a major airport",
        "description": "Search for a flight gateway followed by a train or bus to Kyoto.",
        "value": {
            "current_location": "Bangalore, India",
            "destination": "Kyoto, Japan",
            "start_date": "2026-10-10",
            "end_date": "2026-10-17",
            "budget_min": 150000,
            "budget_max": 300000,
            "currency": "INR",
            "interests": ["live music", "Japanese culture", "food", "temples"],
            "travelers": 1,
            "preferences": ["public transport", "centrally located hotel"],
            "transport_modes": ["flight", "train", "bus"],
            "allow_transport_connections": True,
            "include_premium_fares": False,
        },
    },
    "direct_destination": {
        "summary": "Direct or single-mode route",
        "description": "Prefer a direct train or flight and disable gateway connections.",
        "value": {
            "current_location": "London, United Kingdom",
            "destination": "Paris, France",
            "start_date": "2026-11-05",
            "end_date": "2026-11-09",
            "budget_min": 1200,
            "budget_max": 2000,
            "currency": "GBP",
            "interests": ["art", "food", "architecture"],
            "travelers": 2,
            "preferences": ["central hotel", "public transport"],
            "transport_modes": ["train", "flight"],
            "allow_transport_connections": False,
            "include_premium_fares": False,
        },
    },
}

REVIEW_EXAMPLES = {
    "approve": {
        "summary": "Approve the within-budget plan",
        "value": {"action": "approve", "feedback": "Ready to finalize."},
    },
    "approve_stretch": {
        "summary": "Approve the optional stretch plan",
        "value": {
            "action": "approve",
            "plan_choice": "stretch",
            "feedback": "The additional cost is acceptable.",
        },
    },
    "reject": {
        "summary": "Reject and request a new draft",
        "value": {
            "action": "reject",
            "feedback": "Add stronger late-evening transit safety research.",
        },
    },
    "modify": {
        "summary": "Modify selected plan details",
        "value": {
            "action": "modify",
            "feedback": "Make day two slower.",
            "modifications": {
                "hotel_preference": "Prefer a quiet hotel near the station.",
                "day_changes": [
                    {
                        "day": 2,
                        "replace_activities_with": ["Tea ceremony", "Riverside walk"],
                        "note": "Keep the afternoon low-key.",
                    }
                ],
            },
        },
    },
}


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.plan_service = TravelPlanService(resolved_settings)
        yield
        app.state.plan_service.close()

    api = FastAPI(
        title=resolved_settings.app_name,
        version="1.0.0",
        description=(
            "Multi-agent destination research and itinerary planning with durable human approval."
        ),
        lifespan=lifespan,
    )

    def service() -> TravelPlanService:
        return api.state.plan_service

    @api.get("/health", response_model=HealthResponse, tags=["operations"])
    def health() -> HealthResponse:
        return HealthResponse(app=resolved_settings.app_name, mode=resolved_settings.app_mode)

    @api.post(
        "/plan",
        response_model=PlanAccepted,
        status_code=status.HTTP_201_CREATED,
        tags=["plans"],
    )
    def create_plan(
        request: Annotated[TravelRequest, Body(openapi_examples=PLAN_EXAMPLES)],
    ) -> PlanAccepted:
        try:
            return service().create_plan(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.get("/plan/{plan_id}", response_model=PlanResponse, tags=["plans"])
    def get_plan(plan_id: str) -> PlanResponse:
        try:
            return service().get_plan(plan_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/plan/{plan_id}/review", response_model=PlanResponse, tags=["plans"])
    def review_plan(
        plan_id: str,
        review: Annotated[ReviewRequest, Body(openapi_examples=REVIEW_EXAMPLES)],
    ) -> PlanResponse:
        try:
            return service().review_plan(plan_id, review)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.get("/plan/{plan_id}/final", response_model=FinalPlan, tags=["plans"])
    def get_final_plan(plan_id: str) -> FinalPlan:
        try:
            return service().get_final_plan(plan_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return api


app = create_app()
