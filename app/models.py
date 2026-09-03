from datetime import date, datetime, timedelta
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
    current_location: str = Field(
        min_length=2,
        max_length=120,
        description="Departure city/region and country; do not provide a street address.",
        examples=["Bengaluru, India"],
    )
    destination: str = Field(
        min_length=2,
        max_length=120,
        description="Destination city/region and country.",
        examples=["Kyoto, Japan"],
    )
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
        if any(len(value) > 100 for value in normalized):
            raise ValueError("each interest must contain at most 100 characters")
        self.interests = list(dict.fromkeys(normalized))
        preferences = [value.strip() for value in self.preferences if value.strip()]
        if any(len(value) > 200 for value in preferences):
            raise ValueError("each preference must contain at most 200 characters")
        self.preferences = list(dict.fromkeys(preferences))
        self.current_location = self.current_location.strip()
        self.destination = self.destination.strip()
        if len(self.current_location) < 2:
            raise ValueError("current_location must contain at least 2 characters")
        if len(self.destination) < 2:
            raise ValueError("destination must contain at least 2 characters")
        return self

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


class DayModification(StrictModel):
    day: int = Field(ge=1, le=21)
    replace_activities_with: list[str] = Field(default_factory=list, max_length=8)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def normalize_change(self) -> "DayModification":
        activities = [value.strip() for value in self.replace_activities_with if value.strip()]
        if any(len(value) > 200 for value in activities):
            raise ValueError("each replacement activity must contain at most 200 characters")
        self.replace_activities_with = list(dict.fromkeys(activities))
        self.note = self.note.strip() if self.note and self.note.strip() else None
        if not self.replace_activities_with and self.note is None:
            raise ValueError("a day change requires replacement activities, a note, or both")
        return self


class PlanModification(StrictModel):
    hotel_preference: str | None = Field(default=None, max_length=300)
    day_changes: list[DayModification] = Field(default_factory=list, max_length=21)

    @model_validator(mode="after")
    def require_change(self) -> "PlanModification":
        self.hotel_preference = (
            self.hotel_preference.strip()
            if self.hotel_preference and self.hotel_preference.strip()
            else None
        )
        days = [change.day for change in self.day_changes]
        if len(days) != len(set(days)):
            raise ValueError("only one modification is allowed for each day")
        if self.hotel_preference is None and not self.day_changes:
            raise ValueError("at least one modification must be supplied")
        return self


class ReviewRequest(StrictModel):
    action: ReviewAction
    feedback: str | None = Field(default=None, max_length=2000)
    modifications: PlanModification | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ReviewRequest":
        self.feedback = self.feedback.strip() if self.feedback and self.feedback.strip() else None
        if self.action == ReviewAction.REJECT and self.feedback is None:
            raise ValueError("feedback is required when rejecting a plan")
        if self.action == ReviewAction.MODIFY and self.modifications is None:
            raise ValueError("modifications are required when action is modify")
        if self.action != ReviewAction.MODIFY and self.modifications is not None:
            raise ValueError(f"modifications are not accepted when action is {self.action.value}")
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
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    category: str = Field(min_length=1, max_length=100)
    estimated_cost: float = Field(ge=0)
    source_url: HttpUrl | None = None


class ItineraryDay(StrictModel):
    day: int = Field(ge=1, le=21)
    date: date
    theme: str
    activities: list[Activity] = Field(min_length=1)
    daily_notes: list[str] = Field(default_factory=list)
    estimated_total: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_schedule(self) -> "ItineraryDay":
        times = [activity.time for activity in self.activities]
        if len(times) != len(set(times)):
            raise ValueError("activities within a day cannot have duplicate times")
        expected_total = round(sum(item.estimated_cost for item in self.activities), 2)
        if abs(self.estimated_total - expected_total) > 0.01:
            raise ValueError("estimated_total must equal the sum of activity costs")
        return self


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
    title: str = Field(min_length=1, max_length=200)
    current_location: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    start_date: date
    end_date: date
    travelers: int = Field(ge=1, le=20)
    lodging_notes: list[str]
    days: list[ItineraryDay] = Field(min_length=1, max_length=21)
    budget: BudgetBreakdown
    packing_list: list[str]
    assumptions: list[str]

    @model_validator(mode="after")
    def validate_trip_coverage(self) -> "DraftPlan":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        expected_days = (self.end_date - self.start_date).days + 1
        if len(self.days) != expected_days:
            raise ValueError("itinerary must contain exactly one entry for every trip date")
        for index, itinerary_day in enumerate(self.days, start=1):
            expected_date = self.start_date + timedelta(days=index - 1)
            if itinerary_day.day != index or itinerary_day.date != expected_date:
                raise ValueError("itinerary days must be sequential and match the trip dates")
        return self


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
