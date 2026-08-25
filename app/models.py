from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanStatus(StrEnum):
    VALIDATING = "validating"
    RESEARCHING = "researching"
    PLANNING = "planning"
    AWAITING_REVIEW = "awaiting_review"
    REVISING = "revising"
    FINALIZED = "finalized"
    FAILED = "failed"


class ReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


class TravelRequest(StrictModel):
    destination: str = Field(min_length=2, max_length=120, examples=["Kyoto, Japan"])
    start_date: date
    end_date: date
    budget_min: float = Field(ge=0, examples=[1800])
    budget_max: float = Field(gt=0, examples=[2600])
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    interests: list[str] = Field(min_length=1, max_length=12)
    travelers: int = Field(default=1, ge=1, le=20)
    preferences: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_trip(self) -> "TravelRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 20:
            raise ValueError("trip duration cannot exceed 21 days")
        if self.budget_min > self.budget_max:
            raise ValueError("budget_min cannot exceed budget_max")
        normalized = [value.strip() for value in self.interests if value.strip()]
        if not normalized:
            raise ValueError("at least one non-empty interest is required")
        self.interests = list(dict.fromkeys(normalized))
        self.preferences = list(
            dict.fromkeys(value.strip() for value in self.preferences if value.strip())
        )
        self.destination = self.destination.strip()
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class DayModification(StrictModel):
    day: int = Field(ge=1, le=21)
    replace_activities_with: list[str] = Field(default_factory=list, max_length=8)
    note: str | None = Field(default=None, max_length=500)


class PlanModification(StrictModel):
    hotel_preference: str | None = Field(default=None, max_length=300)
    day_changes: list[DayModification] = Field(default_factory=list, max_length=21)

    @model_validator(mode="after")
    def require_change(self) -> "PlanModification":
        values = self.model_dump(exclude_none=True)
        if not any(value for value in values.values()):
            raise ValueError("at least one modification must be supplied")
        return self


class ReviewRequest(StrictModel):
    action: ReviewAction
    feedback: str | None = Field(default=None, max_length=2000)
    modifications: PlanModification | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ReviewRequest":
        if self.action == ReviewAction.REJECT and not (self.feedback and self.feedback.strip()):
            raise ValueError("feedback is required when rejecting a plan")
        if self.action == ReviewAction.MODIFY and self.modifications is None:
            raise ValueError("modifications are required when action is modify")
        if self.action == ReviewAction.APPROVE and self.modifications is not None:
            raise ValueError("modifications are not accepted when approving a plan")
        self.feedback = self.feedback.strip() if self.feedback else None
        return self


class SearchResult(StrictModel):
    title: str
    url: HttpUrl | None = None
    snippet: str
    source: str


class WeatherSummary(StrictModel):
    source: str
    summary: str
    average_high_c: float | None = None
    average_low_c: float | None = None
    precipitation_probability_max: int | None = Field(default=None, ge=0, le=100)


class ResearchReport(StrictModel):
    destination: str
    summary: str
    attractions: list[str]
    local_tips: list[str]
    safety_notes: list[str]
    season_notes: list[str]
    weather: WeatherSummary
    search_results: list[SearchResult]


class Activity(StrictModel):
    time: str
    title: str
    description: str
    category: str
    estimated_cost: float = Field(ge=0)
    source_url: HttpUrl | None = None


class ItineraryDay(StrictModel):
    day: int = Field(ge=1, le=21)
    date: date
    theme: str
    activities: list[Activity] = Field(min_length=1)
    daily_notes: list[str] = Field(default_factory=list)
    estimated_total: float = Field(ge=0)


class BudgetBreakdown(StrictModel):
    total_budget: float = Field(gt=0)
    lodging: float = Field(ge=0)
    food: float = Field(ge=0)
    activities: float = Field(ge=0)
    local_transport: float = Field(ge=0)
    contingency: float = Field(ge=0)
    per_person: float = Field(ge=0)
    currency: str


class DraftPlan(StrictModel):
    title: str
    destination: str
    start_date: date
    end_date: date
    travelers: int
    lodging_notes: list[str]
    days: list[ItineraryDay]
    budget: BudgetBreakdown
    packing_list: list[str]
    assumptions: list[str]


class ReviewRecord(StrictModel):
    action: ReviewAction
    feedback: str | None = None
    modifications: dict[str, Any] | None = None
    submitted_at: datetime


class FinalPlan(DraftPlan):
    plan_id: str
    finalized_at: datetime
    approval_note: str


class PlanAccepted(StrictModel):
    plan_id: str
    status: PlanStatus
    message: str
    status_url: str
    review_url: str


class PlanResponse(StrictModel):
    plan_id: str
    status: PlanStatus
    request: TravelRequest
    research: ResearchReport | None = None
    draft_plan: DraftPlan | None = None
    final_plan: FinalPlan | None = None
    review_history: list[ReviewRecord] = Field(default_factory=list)
    revision_count: int = 0
    awaiting_input: dict[str, Any] | None = None
    error: str | None = None


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    app: str
    mode: Literal["demo", "live"]
