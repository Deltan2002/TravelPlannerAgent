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
        prompt = (
            f"Travel request:\n{request.model_dump_json(indent=2)}\n\n"
            f"Research report:\n{research.model_dump_json(indent=2)}\n\n"
            f"Budget tool output:\n{budget.model_dump_json(indent=2)}\n\n"
            f"Packing tool output:\n{packing}"
        )
        if feedback:
            prompt += f"\n\nReviewer feedback:\n{feedback}"
        if parsed_modifications:
            modifications_json = parsed_modifications.model_dump_json(indent=2)
            prompt += f"\n\nRequired modifications:\n{modifications_json}"
        generated = self.llm.generate(
            system_prompt=(
                "You are an itinerary planner. Return a feasible day-by-day plan matching every "
                "date exactly once, respect the budget, use realistic travel buffers, and do not "
                "claim reservations were made."
            ),
            user_prompt=prompt,
            output_model=DraftPlan,
            schema_name="travel_itinerary",
        )
        plan = generated or self._deterministic_plan(
            request, research, budget, packing, feedback
        )
        if parsed_modifications:
            plan = self._apply_plan_modifications(plan, parsed_modifications)
        return plan

    @staticmethod
    def _deterministic_plan(request, research, budget, packing, feedback) -> DraftPlan:
        source_urls = [str(result.url) for result in research.search_results if result.url]
        activity_budget_per_day = budget.activities / request.days
        time_slots = ["09:00", "11:30", "14:30", "18:30"]
        interests = request.interests
        days: list[ItineraryDay] = []
        for index in range(request.days):
            interest = interests[index % len(interests)]
            titles = [
                f"Explore a signature {interest} area",
                "Local lunch and neighborhood walk",
                "Cultural highlight or viewpoint",
                "Evening food or arts experience",
            ][:3]
            if index == 0:
                titles[0] = "Arrival, check-in, and neighborhood orientation"
            if index == request.days - 1:
                titles[-1] = "Flexible closing activity and departure preparation"
            per_activity = round(activity_budget_per_day / max(len(titles), 1), 2)
            activities = [
                Activity(
                    time=time_slots[position],
                    title=title,
                    description=(
                        f"A practical option in {request.destination}; confirm hours, transit, "
                        "accessibility, and availability before booking."
                    ),
                    category=interest if position == 0 else "local experience",
                    estimated_cost=per_activity,
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
                    ],
                    estimated_total=round(sum(item.estimated_cost for item in activities), 2),
                )
            )
        assumptions = [
            "International/intercity transport to the destination is excluded from the budget.",
            "Costs are planning estimates, not quotes or confirmed reservations.",
            "Travel times and opening hours must be verified against final venues and dates.",
        ]
        if feedback:
            assumptions.append(f"This revision addresses reviewer feedback: {feedback[:300]}")
        return DraftPlan(
            title=f"{request.days}-day {request.destination} itinerary",
            destination=request.destination,
            start_date=request.start_date,
            end_date=request.end_date,
            travelers=request.travelers,
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
                each_cost = round(existing_total / len(change.replace_activities_with), 2)
                day["activities"] = [
                    {
                        "time": f"{9 + position * 3:02d}:00",
                        "title": title,
                        "description": "Reviewer-requested activity; verify operational details.",
                        "category": "reviewer modification",
                        "estimated_cost": each_cost,
                        "source_url": None,
                    }
                    for position, title in enumerate(change.replace_activities_with)
                ]
                day["estimated_total"] = round(
                    sum(item["estimated_cost"] for item in day["activities"]), 2
                )
        return DraftPlan.model_validate(values)
