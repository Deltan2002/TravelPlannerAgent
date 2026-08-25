import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status

from app.config import Settings, get_settings
from app.models import (
    FinalPlan,
    HealthResponse,
    PlanAccepted,
    PlanResponse,
    ReviewRequest,
    TravelRequest,
)
from app.service import (
    PlanNotFoundError,
    PlanStateConflictError,
    TravelPlanService,
    WorkflowExecutionError,
)


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
    def create_plan(request: TravelRequest) -> PlanAccepted:
        try:
            return service().create_plan(request)
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.get("/plan/{plan_id}", response_model=PlanResponse, tags=["plans"])
    def get_plan(plan_id: str) -> PlanResponse:
        try:
            return service().get_plan(plan_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/plan/{plan_id}/review", response_model=PlanResponse, tags=["plans"])
    def review_plan(plan_id: str, review: ReviewRequest) -> PlanResponse:
        try:
            return service().review_plan(plan_id, review)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanStateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @api.get("/plan/{plan_id}/final", response_model=FinalPlan, tags=["plans"])
    def get_final_plan(plan_id: str) -> FinalPlan:
        try:
            return service().get_final_plan(plan_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanStateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return api


app = create_app()
