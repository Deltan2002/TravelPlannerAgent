from datetime import timedelta

from app.llm import StructuredLLM
from app.models import (
    Activity,
    DraftPlan,
    ItineraryDay,
    PlanModification,
    ResearchReport,
    TravelRequest,
)
from app.prompts import ITINERARY_SYSTEM_PROMPT, build_itinerary_prompt
from app.tools.planning import BudgetAllocatorTool, PackingListTool


class ItineraryPlannerAgent:
    def __init__(
        self,
        budget_allocator: BudgetAllocatorTool,
        packing_list: PackingListTool,
        llm: StructuredLLM,
    ) -> None:
        self.budget_allocator = budget_allocator
        self.packing_list = packing_list
        self.llm = llm

    def run(
        self,
        request: TravelRequest,
        research: ResearchReport,
        *,
        feedback: str | None = None,
        modifications: dict | None = None,
    ) -> DraftPlan:
        parsed_modifications = (
            PlanModification.model_validate(modifications) if modifications else None
        )
        budget = self.budget_allocator.allocate(request)
        packing = self.packing_list.generate(request, research.weather)
        prompt = build_itinerary_prompt(
            request,
            research,
            budget,
            packing,
            feedback,
            parsed_modifications,
        )
        try:
            generated = self.llm.generate(
                system_prompt=ITINERARY_SYSTEM_PROMPT,
                user_prompt=prompt,
                output_model=DraftPlan,
                schema_name="travel_itinerary",
            )
        except ValueError:
            generated = None
        plan = self._prepare_generated_plan(request, research, budget, packing, generated)
        if plan is None:
            plan = self._deterministic_plan(request, research, budget, packing, feedback)
        if parsed_modifications:
            plan = self._apply_plan_modifications(plan, parsed_modifications)
        return plan

    @staticmethod
    def _prepare_generated_plan(request, research, budget, packing, plan) -> DraftPlan | None:
        if plan is None:
            return None
        if plan.start_date != request.start_date or plan.end_date != request.end_date:
            return None
        activity_total = round(sum(day.estimated_total for day in plan.days), 2)
        if activity_total > budget.activities + 0.01:
            return None
        allowed_urls = {str(result.url) for result in research.search_results if result.url}
        values = plan.model_dump()
        for day in values["days"]:
            for activity in day["activities"]:
                source_url = activity.get("source_url")
                if source_url and str(source_url) not in allowed_urls:
                    activity["source_url"] = None
        values.update(
            {
                "current_location": request.current_location,
                "destination": request.destination,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "travelers": request.travelers,
                "transport_summary": research.transport_summary,
                "transport_options": [
                    option.model_dump() for option in research.transport_options
                ],
                "budget": budget.model_dump(),
                "packing_list": packing,
            }
        )
        return DraftPlan.model_validate(values)

    @staticmethod
    def _deterministic_plan(request, research, budget, packing, feedback) -> DraftPlan:
        source_urls = [str(result.url) for result in research.search_results if result.url]
        time_slots = ["09:00", "11:30", "14:30", "18:30"]
        interests = request.interests
        preferences = " ".join(request.preferences).lower()
        activity_count = 3
        if any(value in preferences for value in ("slow", "relaxed", "low-key")):
            activity_count = 2
        elif any(value in preferences for value in ("fast", "packed", "busy")):
            activity_count = 4
        preference_notes: list[str] = []
        if any(value in preferences for value in ("wheelchair", "accessible", "mobility")):
            preference_notes.append(
                "Confirm step-free access, accessible transport, and suitable rest stops."
            )
        if any(
            value in preferences
            for value in ("vegetarian", "vegan", "halal", "kosher", "allergy", "diet")
        ):
            preference_notes.append("Confirm dietary requirements directly with each food venue.")
        if any(value in preferences for value in ("public transport", "no car", "transit")):
            preference_notes.append("Prioritize activities connected by public transport.")
        days: list[ItineraryDay] = []
        remaining_activity_budget = budget.activities
        for index in range(request.days):
            interest = interests[index % len(interests)]
            titles = [
                f"Explore a signature {interest} area",
                "Local lunch and neighborhood walk",
                "Cultural highlight or viewpoint",
                "Evening food or arts experience",
            ][:activity_count]
            if index == 0:
                titles[0] = "Arrival, check-in, and neighborhood orientation"
            if index == request.days - 1:
                titles[-1] = "Flexible closing activity and departure preparation"
            remaining_days = request.days - index
            daily_budget = round(remaining_activity_budget / remaining_days, 2)
            remaining_activity_budget = round(remaining_activity_budget - daily_budget, 2)
            per_activity = round(daily_budget / len(titles), 2)
            activity_costs = [per_activity] * len(titles)
            activity_costs[-1] = round(daily_budget - sum(activity_costs[:-1]), 2)
            activities = [
                Activity(
                    time=time_slots[position],
                    title=title,
                    description=(
                        f"A practical option in {request.destination}; confirm hours, transit, "
                        "accessibility, and availability before booking."
                    ),
                    category=interest if position == 0 else "local experience",
                    estimated_cost=activity_costs[position],
                    source_url=(
                        source_urls[(index + position) % len(source_urls)]
                        if source_urls
                        else None
                    ),
                )
                for position, title in enumerate(titles)
            ]
            days.append(
                ItineraryDay(
                    day=index + 1,
                    date=request.start_date + timedelta(days=index),
                    theme=f"{interest.title()} and local context",
                    activities=activities,
                    daily_notes=[
                        research.local_tips[index % len(research.local_tips)],
                        "Leave 30-45 minutes between major stops for local transit.",
                        *preference_notes,
                    ],
                    estimated_total=round(sum(item.estimated_cost for item in activities), 2),
                )
            )
        assumptions = [
            f"The budget reserves up to {request.origin_transport_budget:.2f} {request.currency} "
            f"for round-trip travel from {request.current_location} to {request.destination} "
            f"for all travelers.",
            "Transport prices are search-derived estimates and must be verified before booking.",
            "Costs are planning estimates, not quotes or confirmed reservations.",
            "Travel times and opening hours must be verified against final venues and dates.",
        ]
        if feedback:
            assumptions.append(f"This revision addresses reviewer feedback: {feedback[:300]}")
        if request.preferences:
            assumptions.append(f"User preferences: {', '.join(request.preferences)}.")
        return DraftPlan(
            title=f"{request.days}-day {request.destination} itinerary",
            current_location=request.current_location,
            destination=request.destination,
            start_date=request.start_date,
            end_date=request.end_date,
            travelers=request.travelers,
            transport_summary=research.transport_summary,
            transport_options=research.transport_options,
            lodging_notes=[
                "Choose a well-connected area that reduces daily transit.",
                "Confirm cancellation terms, taxes, room occupancy, and accessibility directly.",
            ],
            days=days,
            budget=budget,
            packing_list=packing,
            assumptions=assumptions,
        )

    @staticmethod
    def _apply_plan_modifications(
        plan: DraftPlan, modifications: PlanModification
    ) -> DraftPlan:
        values = plan.model_dump()
        if modifications.hotel_preference:
            values["lodging_notes"].insert(0, modifications.hotel_preference)
        indexed_changes = {change.day: change for change in modifications.day_changes}
        for day in values["days"]:
            change = indexed_changes.get(day["day"])
            if change is None:
                continue
            if change.note:
                day["daily_notes"].insert(0, change.note)
            if change.replace_activities_with:
                existing_total = day["estimated_total"]
                activity_count = len(change.replace_activities_with)
                each_cost = round(existing_total / activity_count, 2)
                replacement_costs = [each_cost] * activity_count
                replacement_costs[-1] = round(
                    existing_total - sum(replacement_costs[:-1]), 2
                )
                replacement_times = [
                    "08:00",
                    "10:00",
                    "12:00",
                    "14:00",
                    "16:00",
                    "18:00",
                    "20:00",
                    "22:00",
                ]
                day["activities"] = [
                    {
                        "time": replacement_times[position],
                        "title": title,
                        "description": "Reviewer-requested activity; verify operational details.",
                        "category": "reviewer modification",
                        "estimated_cost": replacement_costs[position],
                        "source_url": None,
                    }
                    for position, title in enumerate(change.replace_activities_with)
                ]
                day["estimated_total"] = round(
                    sum(item["estimated_cost"] for item in day["activities"]), 2
                )
        return DraftPlan.model_validate(values)
