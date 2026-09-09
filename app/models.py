from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

STRETCH_BUDGET_MULTIPLIER = 1.15
TransportMode = Literal["flight", "train", "bus", "ferry", "car"]
CabinClass = Literal["economy", "premium_economy", "business", "first", "unspecified"]
FareDateBasis = Literal["exact_dates", "partial_dates", "dates_not_shown"]


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
    budget_min: float = Field(
        ge=0, description="Minimum total trip budget, including origin transport.", examples=[1800]
    )
    budget_max: float = Field(
        gt=0, description="Maximum total trip budget, including origin transport.", examples=[2600]
    )
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    interests: list[str] = Field(min_length=1, max_length=12)
    travelers: int = Field(default=1, ge=1, le=20)
    preferences: list[str] = Field(default_factory=list, max_length=12)
    transport_modes: list[TransportMode] = Field(
        default_factory=lambda: ["flight", "train", "bus", "ferry", "car"],
        min_length=1,
        max_length=5,
        description="Transport modes the planner may use for direct and connected routes.",
    )
    allow_transport_connections: bool = Field(
        default=True,
        description="Allow gateway routes such as flight to Tokyo followed by train to Kyoto.",
    )
    include_premium_fares: bool = Field(
        default=False,
        description=(
            "Include premium-economy, business-class, and first-class flight fares. "
            "False keeps flight results economy-only."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def remove_legacy_transport_budget(cls, value: Any) -> Any:
        if isinstance(value, dict) and "origin_transport_budget" in value:
            value = dict(value)
            value.pop("origin_transport_budget")
        return value

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
        self.transport_modes = list(dict.fromkeys(self.transport_modes))
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
    plan_choice: Literal["within_budget", "stretch"] | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ReviewRequest":
        self.feedback = self.feedback.strip() if self.feedback and self.feedback.strip() else None
        if self.action == ReviewAction.REJECT and self.feedback is None:
            raise ValueError("feedback is required when rejecting a plan")
        if self.action == ReviewAction.MODIFY and self.modifications is None:
            raise ValueError("modifications are required when action is modify")
        if self.action != ReviewAction.MODIFY and self.modifications is not None:
            raise ValueError(f"modifications are not accepted when action is {self.action.value}")
        if self.action != ReviewAction.APPROVE and self.plan_choice is not None:
            raise ValueError("plan_choice is only accepted when approving a plan")
        return self


class SearchResult(StrictModel):
    title: str
    url: HttpUrl | None = None
    snippet: str
    source: str


class ConvertedAmount(StrictModel):
    amount: float = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    exchange_rate: float = Field(gt=0)
    rate_date: date | None = None
    source: str = Field(min_length=1, max_length=200)


class TransportLeg(StrictModel):
    mode: TransportMode
    cabin_class: CabinClass = "unspecified"
    fare_date_basis: FareDateBasis = "dates_not_shown"
    origin: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    estimated_cost_per_person: float = Field(gt=0)
    estimated_total_cost: float = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    travelers: int = Field(ge=1, le=20)
    estimated_total_in_budget_currency: float = Field(gt=0)
    budget_currency: str = Field(pattern=r"^[A-Z]{3}$")
    fare_basis: Literal["explicit_per_person", "assumed_per_person"]
    round_trip_basis: Literal["explicit_round_trip", "one_way_doubled"]
    source_title: str = Field(min_length=1, max_length=200)
    source_url: HttpUrl
    price_evidence: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_total(self) -> "TransportLeg":
        expected = round(self.estimated_cost_per_person * self.travelers, 2)
        if abs(self.estimated_total_cost - expected) > 0.01:
            raise ValueError("leg total must equal per-person cost times travelers")
        return self


class UnpricedTransportLeg(StrictModel):
    mode: TransportMode
    origin: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    pricing_status: Literal["price_unavailable"] = "price_unavailable"
    reason: str = Field(min_length=1, max_length=500)


class TransportOption(StrictModel):
    mode: Literal["flight", "train", "bus", "ferry", "car", "multimodal"]
    cabin_class: CabinClass = "unspecified"
    fare_date_basis: FareDateBasis = "dates_not_shown"
    connection_schedule_status: Literal["not_applicable", "not_verified"] = (
        "not_applicable"
    )
    source_title: str = Field(min_length=1, max_length=200)
    route: str = Field(min_length=3, max_length=300)
    gateway: str | None = Field(default=None, min_length=2, max_length=120)
    route_legs: list[str] = Field(default_factory=list, max_length=8)
    price_scope: Literal["complete_route", "primary_leg_only"] = "complete_route"
    fare_basis: Literal["explicit_per_person", "assumed_per_person"] = "explicit_per_person"
    round_trip_basis: Literal["explicit_round_trip", "one_way_doubled"] = (
        "explicit_round_trip"
    )
    connection_note: str | None = Field(default=None, max_length=1000)
    priced_cost_per_person: float = Field(
        gt=0,
        description="Per-person subtotal for priced_legs only; unpriced_legs are excluded.",
    )
    priced_total_cost: float = Field(
        gt=0,
        description=(
            "Subtotal for priced_legs only; this is a full route total only when "
            "price_scope is complete_route."
        ),
    )
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    travelers: int = Field(ge=1, le=20)
    source_url: HttpUrl
    price_evidence: str = Field(min_length=1, max_length=500)
    priced_legs: list[TransportLeg] = Field(
        default_factory=list,
        max_length=8,
        description="Route legs whose fares are included in priced_total_cost.",
    )
    unpriced_legs: list[UnpricedTransportLeg] = Field(
        default_factory=list,
        max_length=8,
        description="Route legs shown separately because no sufficiently reliable fare was found.",
    )
    priced_total_in_destination_currency: ConvertedAmount | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_cost_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        renames = {
            "estimated_cost_per_person": "priced_cost_per_person",
            "estimated_total_cost": "priced_total_cost",
            "estimated_total_in_destination_currency": (
                "priced_total_in_destination_currency"
            ),
        }
        for old_name, new_name in renames.items():
            if old_name in migrated:
                migrated.setdefault(new_name, migrated.pop(old_name))
        if migrated.get("price_scope") != "primary_leg_only":
            return migrated
        if migrated.get("unpriced_legs"):
            return migrated
        priced_count = len(migrated.get("priced_legs") or [])
        unpriced: list[dict[str, str]] = []
        for description in (migrated.get("route_legs") or [])[priced_count:]:
            mode_name, separator, route_text = str(description).partition(":")
            mode = mode_name.strip().lower()
            direction = route_text.split(",", 1)[0].strip()
            origin, route_separator, destination = direction.partition(" to ")
            if (
                separator
                and route_separator
                and mode in {"flight", "train", "bus", "ferry", "car"}
            ):
                unpriced.append(
                    {
                        "mode": mode,
                        "origin": origin.strip(),
                        "destination": destination.strip(),
                        "reason": "No verified fare was stored for this route leg.",
                    }
                )
        if unpriced:
            migrated["unpriced_legs"] = unpriced
        return migrated

    @model_validator(mode="after")
    def validate_total(self) -> "TransportOption":
        expected = round(self.priced_cost_per_person * self.travelers, 2)
        if abs(self.priced_total_cost - expected) > 0.01:
            raise ValueError("priced_total_cost must equal per-person cost times travelers")
        legs = [value.strip() for value in self.route_legs if value.strip()]
        if any(len(value) > 300 for value in legs):
            raise ValueError("each route leg must contain at most 300 characters")
        self.route_legs = list(dict.fromkeys(legs))
        self.connection_note = (
            self.connection_note.strip()
            if self.connection_note and self.connection_note.strip()
            else None
        )
        if self.mode == "multimodal" and len(self.route_legs) < 2:
            raise ValueError("a multimodal option requires at least two route legs")
        if self.mode == "multimodal" and self.connection_schedule_status == "not_applicable":
            self.connection_schedule_status = "not_verified"
        if self.mode != "multimodal" and self.connection_schedule_status != "not_applicable":
            raise ValueError("single-mode routes cannot have a connection schedule status")
        if self.price_scope == "primary_leg_only" and self.connection_note is None:
            raise ValueError("a primary-leg price requires a connection note")
        if self.price_scope == "primary_leg_only" and not self.unpriced_legs:
            raise ValueError("a partial route price requires at least one unpriced leg")
        if self.price_scope == "complete_route" and self.unpriced_legs:
            raise ValueError("a complete route cannot contain unpriced legs")
        if self.priced_legs:
            if any(leg.travelers != self.travelers for leg in self.priced_legs):
                raise ValueError("priced leg travelers must match transport option travelers")
            if any(leg.budget_currency != self.currency for leg in self.priced_legs):
                raise ValueError("priced leg budget currency must match transport option currency")
            priced_total = round(
                sum(leg.estimated_total_in_budget_currency for leg in self.priced_legs),
                2,
            )
            if abs(priced_total - self.priced_total_cost) > 0.01:
                raise ValueError("priced legs must add up to priced_total_cost")
            expected_date_basis = (
                "exact_dates"
                if all(leg.fare_date_basis == "exact_dates" for leg in self.priced_legs)
                else "partial_dates"
                if any(
                    leg.fare_date_basis != "dates_not_shown"
                    for leg in self.priced_legs
                )
                else "dates_not_shown"
            )
            if self.fare_date_basis != expected_date_basis:
                raise ValueError("fare_date_basis must summarize the priced legs")
        return self


class WeatherSummary(StrictModel):
    source: str
    summary: str
    data_type: Literal[
        "live_forecast",
        "historical_climate_estimate",
        "generic_seasonal_guidance",
    ] = "generic_seasonal_guidance"
    average_high_c: float | None = None
    average_low_c: float | None = None
    precipitation_probability_max: int | None = Field(default=None, ge=0, le=100)
    average_daily_precipitation_mm: float | None = Field(default=None, ge=0)
    historical_years: list[int] = Field(default_factory=list, max_length=10)


class ResearchNarrative(StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    attractions: list[str] = Field(min_length=1, max_length=12)
    local_tips: list[str] = Field(min_length=1, max_length=12)
    safety_notes: list[str] = Field(min_length=1, max_length=12)
    season_notes: list[str] = Field(min_length=1, max_length=12)


class ResearchReport(StrictModel):
    destination: str
    summary: str
    attractions: list[str]
    local_tips: list[str]
    safety_notes: list[str]
    season_notes: list[str]
    transport_summary: str = "No transport options were recorded for this plan."
    transport_options: list[TransportOption] = Field(default_factory=list)
    transport_search_results: list[SearchResult] = Field(default_factory=list)
    weather: WeatherSummary
    search_results: list[SearchResult]

    @model_validator(mode="before")
    @classmethod
    def remove_legacy_generation_source(cls, value: Any) -> Any:
        if isinstance(value, dict) and "generation_source" in value:
            value = dict(value)
            value.pop("generation_source")
        return value


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


class ItineraryContent(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    lodging_notes: list[str] = Field(min_length=1, max_length=12)
    days: list[ItineraryDay] = Field(min_length=1, max_length=21)
    assumptions: list[str] = Field(min_length=1, max_length=20)


class BudgetBreakdown(StrictModel):
    basis: Literal["spending_allocation_not_booking_quote"] = (
        "spending_allocation_not_booking_quote"
    )
    total_budget: float = Field(gt=0)
    origin_transport: float = Field(ge=0)
    lodging: float = Field(ge=0)
    food: float = Field(ge=0)
    activities: float = Field(ge=0)
    local_transport: float = Field(ge=0)
    contingency: float = Field(ge=0)
    per_person: float = Field(ge=0)
    currency: str

    @model_validator(mode="after")
    def validate_allocation(self) -> "BudgetBreakdown":
        allocated = round(
            self.origin_transport
            + self.lodging
            + self.food
            + self.activities
            + self.local_transport
            + self.contingency,
            2,
        )
        if abs(allocated - self.total_budget) > 0.01:
            raise ValueError("budget categories must add up to total_budget")
        return self


class DraftPlan(StrictModel):
    scenario: Literal["within_budget", "stretch"] = "within_budget"
    over_budget_by: float = Field(default=0, ge=0)
    requested_budget_min: float | None = Field(default=None, ge=0)
    requested_budget_max: float | None = Field(default=None, gt=0)
    title: str = Field(min_length=1, max_length=200)
    current_location: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    start_date: date
    end_date: date
    travelers: int = Field(ge=1, le=20)
    transport_summary: str = "No transport options were recorded for this plan."
    transport_options: list[TransportOption] = Field(default_factory=list)
    selected_transport: TransportOption | None = None
    lodging_notes: list[str]
    days: list[ItineraryDay] = Field(min_length=1, max_length=21)
    budget: BudgetBreakdown
    packing_list: list[str]
    assumptions: list[str]

    @model_validator(mode="before")
    @classmethod
    def remove_legacy_generation_source(cls, value: Any) -> Any:
        if isinstance(value, dict) and "generation_source" in value:
            value = dict(value)
            value.pop("generation_source")
        return value

    @model_validator(mode="after")
    def validate_trip_coverage(self) -> "DraftPlan":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.requested_budget_min is not None and self.requested_budget_max is not None:
            if self.requested_budget_min > self.requested_budget_max:
                raise ValueError("requested budget minimum cannot exceed maximum")
            if self.scenario == "within_budget":
                if self.budget.total_budget > self.requested_budget_max + 0.01:
                    raise ValueError("within-budget plan exceeds requested maximum")
                if self.over_budget_by != 0:
                    raise ValueError("within-budget plan cannot have an over-budget amount")
            else:
                stretch_limit = round(
                    self.requested_budget_max * STRETCH_BUDGET_MULTIPLIER,
                    2,
                )
                if not self.requested_budget_max < self.budget.total_budget <= stretch_limit:
                    raise ValueError("stretch plan is outside its allowed budget range")
                expected_overage = round(
                    self.budget.total_budget - self.requested_budget_max,
                    2,
                )
                if abs(self.over_budget_by - expected_overage) > 0.01:
                    raise ValueError("stretch plan over_budget_by is inconsistent")
        expected_days = (self.end_date - self.start_date).days + 1
        if len(self.days) != expected_days:
            raise ValueError("itinerary must contain exactly one entry for every trip date")
        for index, itinerary_day in enumerate(self.days, start=1):
            expected_date = self.start_date + timedelta(days=index - 1)
            if itinerary_day.day != index or itinerary_day.date != expected_date:
                raise ValueError("itinerary days must be sequential and match the trip dates")
        totals = [option.priced_total_cost for option in self.transport_options]
        if totals != sorted(totals):
            raise ValueError("transport_options must be sorted by priced total cost")
        expected_per_person = round(self.budget.total_budget / self.travelers, 2)
        if abs(self.budget.per_person - expected_per_person) > 0.01:
            raise ValueError("budget per_person must equal total_budget divided by travelers")
        for option in self.transport_options:
            if option.travelers != self.travelers:
                raise ValueError("transport option travelers must match plan travelers")
            if option.currency != self.budget.currency:
                raise ValueError("transport option currency must match the plan budget")
        if self.selected_transport is not None:
            if self.selected_transport not in self.transport_options:
                raise ValueError("selected_transport must be present in transport_options")
            if (
                abs(
                    self.budget.origin_transport
                    - self.selected_transport.priced_total_cost
                )
                > 0.01
            ):
                raise ValueError("budget origin_transport must match selected_transport")
        return self


class ReadinessCheck(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    status: Literal["pass", "warning", "blocker"]
    message: str = Field(min_length=1, max_length=500)
    weight: float = Field(gt=0, le=100)
    awarded_points: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_points(self) -> "ReadinessCheck":
        if self.awarded_points > self.weight:
            raise ValueError("awarded readiness points cannot exceed the check weight")
        return self


class PlanReadinessEvaluation(StrictModel):
    scenario: Literal["within_budget", "stretch"]
    score: float = Field(ge=0, le=100)
    status: Literal["ready", "needs_verification", "blocked"]
    summary: str = Field(min_length=1, max_length=500)
    warning_count: int = Field(ge=0)
    blocker_count: int = Field(ge=0)
    checks: list[ReadinessCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evaluation(self) -> "PlanReadinessEvaluation":
        total_weight = round(sum(check.weight for check in self.checks), 2)
        if total_weight != 100:
            raise ValueError("readiness check weights must add up to 100")
        expected_score = round(sum(check.awarded_points for check in self.checks), 2)
        if abs(self.score - expected_score) > 0.01:
            raise ValueError("readiness score must equal awarded check points")
        warnings = sum(check.status == "warning" for check in self.checks)
        blockers = sum(check.status == "blocker" for check in self.checks)
        if self.warning_count != warnings or self.blocker_count != blockers:
            raise ValueError("readiness warning and blocker counts must match the checks")
        expected_status = (
            "blocked" if blockers else "needs_verification" if warnings else "ready"
        )
        if self.status != expected_status:
            raise ValueError("readiness status is inconsistent with its checks")
        return self


class ReviewRecord(StrictModel):
    action: ReviewAction
    feedback: str | None = None
    modifications: dict[str, Any] | None = None
    plan_choice: Literal["within_budget", "stretch"] | None = None
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
    draft_readiness: PlanReadinessEvaluation | None = None
    stretch_plan: DraftPlan | None = None
    stretch_readiness: PlanReadinessEvaluation | None = None
    final_plan: FinalPlan | None = None
    review_history: list[ReviewRecord] = Field(default_factory=list)
    revision_count: int = 0
    awaiting_input: dict[str, Any] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_budget_scenarios(self) -> "PlanResponse":
        if self.draft_readiness is not None:
            if self.draft_plan is None or self.draft_readiness.scenario != "within_budget":
                raise ValueError("draft readiness requires a within-budget draft plan")
        if self.stretch_readiness is not None:
            if self.stretch_plan is None or self.stretch_readiness.scenario != "stretch":
                raise ValueError("stretch readiness requires a stretch plan")
        if self.draft_plan is not None:
            if self.draft_plan.scenario != "within_budget":
                raise ValueError("draft_plan must be the within_budget scenario")
            if self.draft_plan.budget.currency != self.request.currency:
                raise ValueError("within-budget plan currency must match the request")
            if self.draft_plan.budget.total_budget > self.request.budget_max + 0.01:
                raise ValueError("within-budget plan exceeds budget_max")
            if self.draft_plan.over_budget_by != 0:
                raise ValueError("within-budget plan cannot have an over-budget amount")
        if self.stretch_plan is not None:
            maximum = round(
                self.request.budget_max * STRETCH_BUDGET_MULTIPLIER,
                2,
            )
            if self.stretch_plan.scenario != "stretch":
                raise ValueError("stretch_plan must be the stretch scenario")
            if self.stretch_plan.budget.currency != self.request.currency:
                raise ValueError("stretch plan currency must match the request")
            if not self.request.budget_max < self.stretch_plan.budget.total_budget <= maximum:
                raise ValueError("stretch plan must be above budget_max and within its cap")
            expected_overage = round(
                self.stretch_plan.budget.total_budget - self.request.budget_max,
                2,
            )
            if abs(self.stretch_plan.over_budget_by - expected_overage) > 0.01:
                raise ValueError("stretch plan over_budget_by is inconsistent")
        return self


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    app: str
    mode: Literal["demo", "live"]
