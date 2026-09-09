import re
from datetime import timedelta

from app.models import (
    STRETCH_BUDGET_MULTIPLIER,
    BudgetBreakdown,
    DraftPlan,
    PlanReadinessEvaluation,
    ReadinessCheck,
    ResearchReport,
    TravelRequest,
    WeatherSummary,
)


class BudgetAllocatorTool:
    def allocate(
        self,
        request: TravelRequest,
        total_budget: float,
        origin_transport: float,
    ) -> BudgetBreakdown:
        total = round(total_budget, 2)
        destination_budget = round(total - origin_transport, 2)
        if destination_budget <= 0:
            raise ValueError("transport cost leaves no budget for the destination")
        lodging = round(destination_budget * 0.35, 2)
        food = round(destination_budget * 0.20, 2)
        activities = round(destination_budget * 0.25, 2)
        local_transport = round(destination_budget * 0.15, 2)
        contingency = round(
            destination_budget - lodging - food - activities - local_transport, 2
        )
        return BudgetBreakdown(
            total_budget=total,
            origin_transport=origin_transport,
            lodging=lodging,
            food=food,
            activities=activities,
            local_transport=local_transport,
            contingency=contingency,
            per_person=round(total / request.travelers, 2),
            currency=request.currency,
        )


class PackingListTool:
    def generate(self, request: TravelRequest, weather: WeatherSummary) -> list[str]:
        items = [
            "Passport/ID and digital booking copies",
            "Comfortable walking shoes",
            "Reusable water bottle",
            "Phone charger and travel adapter",
            f"Clothing for {request.days} days (or a laundry plan)",
        ]
        summary = weather.summary.lower()
        if "rain" in summary or (weather.precipitation_probability_max or 0) >= 30:
            items.append("Compact umbrella or waterproof shell")
        if weather.average_high_c is not None and weather.average_high_c >= 27:
            items.extend(["Sun protection", "Lightweight breathable clothing"])
        if weather.average_low_c is not None and weather.average_low_c <= 10:
            items.append("Warm layers")
        interests = " ".join(request.interests).lower()
        if any(word in interests for word in ("hike", "outdoor", "nature")):
            items.append("Daypack and trail-appropriate footwear")
        if any(word in interests for word in ("photo", "photography")):
            items.append("Camera, spare battery, and memory card")
        return list(dict.fromkeys(items))


class PlanReadinessTool:
    WEIGHTS = {
        "date_coverage": 15.0,
        "budget": 15.0,
        "activity_budget": 10.0,
        "transport_route": 15.0,
        "transport_dates": 10.0,
        "connection_schedule": 10.0,
        "fare_evidence": 10.0,
        "weather_confidence": 5.0,
        "activity_sources": 5.0,
        "preference_coverage": 5.0,
    }

    def evaluate(
        self,
        request: TravelRequest,
        research: ResearchReport,
        plan: DraftPlan,
    ) -> PlanReadinessEvaluation:
        checks = [
            self._date_coverage(request, plan),
            self._budget(request, plan),
            self._activity_budget(plan),
            self._transport_route(plan),
            self._transport_dates(plan),
            self._connection_schedule(plan),
            self._fare_evidence(plan),
            self._weather_confidence(research),
            self._activity_sources(plan),
            self._preference_coverage(request, plan),
        ]
        warning_count = sum(check.status == "warning" for check in checks)
        blocker_count = sum(check.status == "blocker" for check in checks)
        score = round(sum(check.awarded_points for check in checks), 2)
        status = (
            "blocked"
            if blocker_count
            else "needs_verification"
            if warning_count
            else "ready"
        )
        summary = (
            f"Approval is blocked by {blocker_count} critical check(s)."
            if blocker_count
            else f"Review {warning_count} item(s) before booking."
            if warning_count
            else "All automated readiness checks passed."
        )
        return PlanReadinessEvaluation(
            scenario=plan.scenario,
            score=score,
            status=status,
            summary=summary,
            warning_count=warning_count,
            blocker_count=blocker_count,
            checks=checks,
        )

    @classmethod
    def _check(cls, name: str, status: str, message: str) -> ReadinessCheck:
        weight = cls.WEIGHTS[name]
        awarded = weight if status == "pass" else weight / 2 if status == "warning" else 0
        return ReadinessCheck(
            name=name,
            status=status,
            message=message,
            weight=weight,
            awarded_points=awarded,
        )

    @classmethod
    def _date_coverage(cls, request: TravelRequest, plan: DraftPlan) -> ReadinessCheck:
        valid = len(plan.days) == request.days and all(
            day.day == index
            and day.date == request.start_date + timedelta(days=index - 1)
            for index, day in enumerate(plan.days, start=1)
        )
        return cls._check(
            "date_coverage",
            "pass" if valid else "blocker",
            f"All {request.days} requested trip date(s) are covered."
            if valid
            else "The itinerary does not cover every requested date exactly once.",
        )

    @classmethod
    def _budget(cls, request: TravelRequest, plan: DraftPlan) -> ReadinessCheck:
        valid = (
            plan.budget.total_budget <= request.budget_max + 0.01
            if plan.scenario == "within_budget"
            else request.budget_max
            < plan.budget.total_budget
            <= round(request.budget_max * STRETCH_BUDGET_MULTIPLIER, 2)
        )
        return cls._check(
            "budget",
            "pass" if valid else "blocker",
            f"The {plan.scenario.replace('_', '-')} plan respects its allowed budget range."
            if valid
            else "The plan is outside its allowed budget range.",
        )

    @classmethod
    def _activity_budget(cls, plan: DraftPlan) -> ReadinessCheck:
        total = round(sum(day.estimated_total for day in plan.days), 2)
        valid = total <= plan.budget.activities + 0.01
        return cls._check(
            "activity_budget",
            "pass" if valid else "blocker",
            f"Planned activities use {total:.2f} of the {plan.budget.activities:.2f} "
            f"{plan.budget.currency} activity allocation."
            if valid
            else "Planned activity costs exceed the activity allocation.",
        )

    @classmethod
    def _transport_route(cls, plan: DraftPlan) -> ReadinessCheck:
        option = plan.selected_transport
        if option is None:
            return cls._check(
                "transport_route", "blocker", "No origin-to-destination transport was selected."
            )
        if option.price_scope == "primary_leg_only":
            return cls._check(
                "transport_route",
                "warning",
                "The route is usable for planning, but one or more connection legs are unpriced.",
            )
        return cls._check(
            "transport_route", "pass", "Every required route leg has a sourced price estimate."
        )

    @classmethod
    def _transport_dates(cls, plan: DraftPlan) -> ReadinessCheck:
        option = plan.selected_transport
        if option is None:
            return cls._check(
                "transport_dates", "blocker", "Transport dates cannot be checked without a route."
            )
        messages = {
            "exact_dates": "Both requested travel dates appear in the fare evidence.",
            "partial_dates": "Only part of the requested date range appears in the fare evidence.",
            "dates_not_shown": "The fare evidence does not display the requested travel dates.",
        }
        return cls._check(
            "transport_dates",
            "pass" if option.fare_date_basis == "exact_dates" else "warning",
            messages[option.fare_date_basis],
        )

    @classmethod
    def _connection_schedule(cls, plan: DraftPlan) -> ReadinessCheck:
        option = plan.selected_transport
        if option is None:
            return cls._check(
                "connection_schedule", "blocker", "A connection cannot be assessed without a route."
            )
        if option.connection_schedule_status == "not_verified":
            return cls._check(
                "connection_schedule",
                "warning",
                "Separately sourced route legs have not been schedule-matched.",
            )
        return cls._check(
            "connection_schedule", "pass", "No unverified multimodal connection is required."
        )

    @classmethod
    def _fare_evidence(cls, plan: DraftPlan) -> ReadinessCheck:
        option = plan.selected_transport
        if option is None or not option.priced_legs:
            return cls._check(
                "fare_evidence", "blocker", "No priced transport leg has source evidence."
            )
        complete = all(
            leg.source_url and leg.source_title and leg.price_evidence
            for leg in option.priced_legs
        )
        return cls._check(
            "fare_evidence",
            "pass" if complete else "blocker",
            f"All {len(option.priced_legs)} priced transport leg(s) include source evidence."
            if complete
            else "At least one priced transport leg is missing source evidence.",
        )

    @classmethod
    def _weather_confidence(cls, research: ResearchReport) -> ReadinessCheck:
        weather_type = research.weather.data_type
        messages = {
            "live_forecast": "The packing guidance uses a live weather forecast.",
            "historical_climate_estimate": (
                "Weather guidance is based on historical climate, not a live forecast."
            ),
            "generic_seasonal_guidance": (
                "Only generic seasonal weather guidance is available."
            ),
        }
        return cls._check(
            "weather_confidence",
            "pass" if weather_type == "live_forecast" else "warning",
            messages[weather_type],
        )

    @classmethod
    def _activity_sources(cls, plan: DraftPlan) -> ReadinessCheck:
        activities = [activity for day in plan.days for activity in day.activities]
        sourced = sum(activity.source_url is not None for activity in activities)
        coverage = sourced / len(activities) if activities else 0
        status = "pass" if coverage >= 0.5 else "warning"
        return cls._check(
            "activity_sources",
            status,
            f"{sourced} of {len(activities)} planned activities include a research source.",
        )

    @classmethod
    def _preference_coverage(cls, request: TravelRequest, plan: DraftPlan) -> ReadinessCheck:
        if not request.preferences:
            return cls._check(
                "preference_coverage", "pass", "No additional user preferences were supplied."
            )
        plan_text = " ".join(
            [
                plan.title,
                *plan.lodging_notes,
                *plan.assumptions,
                *(day.theme for day in plan.days),
                *(note for day in plan.days for note in day.daily_notes),
                *(activity.description for day in plan.days for activity in day.activities),
            ]
        ).casefold()
        missing = []
        for preference in request.preferences:
            words = [
                word
                for word in re.findall(r"[a-z0-9]+", preference.casefold())
                if len(word) >= 4
            ]
            if words and not any(word in plan_text for word in words):
                missing.append(preference)
        return cls._check(
            "preference_coverage",
            "pass" if not missing else "warning",
            "The itinerary text reflects all supplied preferences."
            if not missing
            else f"Verify these preferences manually: {', '.join(missing)}.",
        )
