import logging
import re
from datetime import timedelta
from typing import Literal

from app.llm import StructuredLLM
from app.models import (
    STRETCH_BUDGET_MULTIPLIER,
    Activity,
    DraftPlan,
    ItineraryContent,
    ItineraryDay,
    PlanModification,
    ResearchReport,
    TransportOption,
    TravelRequest,
)
from app.prompts import ITINERARY_SYSTEM_PROMPT, build_itinerary_prompt
from app.tools.planning import BudgetAllocatorTool, PackingListTool

logger = logging.getLogger(__name__)


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
    ) -> tuple[DraftPlan | None, DraftPlan | None]:
        parsed_modifications = (
            PlanModification.model_validate(modifications) if modifications else None
        )
        if not research.transport_options:
            return None, None
        selected_transport = research.transport_options[0]
        planning_total = round((request.budget_min + request.budget_max) / 2, 2)
        within_budget_plan = None
        if selected_transport.priced_total_cost < request.budget_max:
            within_total = (
                planning_total
                if selected_transport.priced_total_cost < planning_total
                else request.budget_max
            )
            within_budget_plan = self._create_plan(
                request,
                research,
                selected_transport,
                total_budget=within_total,
                scenario="within_budget",
                feedback=feedback,
                modifications=parsed_modifications,
                use_llm=True,
            )
        stretch_total = round(request.budget_max * STRETCH_BUDGET_MULTIPLIER, 2)
        stretch_plan = None
        if selected_transport.priced_total_cost < stretch_total:
            stretch_plan = self._create_plan(
                request,
                research,
                selected_transport,
                total_budget=stretch_total,
                scenario="stretch",
                feedback=feedback,
                modifications=parsed_modifications,
                use_llm=True,
            )
        return within_budget_plan, stretch_plan

    def _create_plan(
        self,
        request: TravelRequest,
        research: ResearchReport,
        selected_transport: TransportOption,
        *,
        total_budget: float,
        scenario: Literal["within_budget", "stretch"],
        feedback: str | None,
        modifications: PlanModification | None,
        use_llm: bool,
    ) -> DraftPlan:
        over_budget_by = round(max(0, total_budget - request.budget_max), 2)
        budget = self.budget_allocator.allocate(
            request,
            total_budget,
            selected_transport.priced_total_cost,
        )
        packing = self.packing_list.generate(request, research.weather)
        generated = None
        if use_llm:
            prompt = build_itinerary_prompt(
                request,
                research,
                budget,
                feedback,
                modifications,
                scenario,
                over_budget_by,
                selected_transport,
            )
            try:
                generated = self.llm.generate(
                    system_prompt=ITINERARY_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    output_model=ItineraryContent,
                    schema_name=f"travel_itinerary_content_{scenario}",
                )
            except (RuntimeError, ValueError) as exc:
                logger.info(
                    "OpenAI itinerary unavailable; continuing with deterministic fallback: %s",
                    exc,
                )
        plan = self._prepare_generated_plan(
            request,
            research,
            budget,
            packing,
            generated,
            selected_transport,
            scenario,
            over_budget_by,
        )
        if plan is None:
            plan = self._deterministic_plan(
                request,
                research,
                budget,
                packing,
                selected_transport,
                scenario,
                over_budget_by,
                feedback,
            )
        if modifications:
            plan = self._apply_plan_modifications(plan, modifications)
        return plan

    @staticmethod
    def _prepare_generated_plan(
        request,
        research,
        budget,
        packing,
        plan,
        selected_transport,
        scenario,
        over_budget_by,
    ) -> DraftPlan | None:
        if plan is None:
            return None
        if len(plan.days) != request.days:
            return None
        if any(
            day.day != index
            or day.date != request.start_date + timedelta(days=index - 1)
            for index, day in enumerate(plan.days, start=1)
        ):
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
                "scenario": scenario,
                "over_budget_by": over_budget_by,
                "requested_budget_min": request.budget_min,
                "requested_budget_max": request.budget_max,
                "current_location": request.current_location,
                "destination": request.destination,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "travelers": request.travelers,
                "transport_summary": research.transport_summary,
                "transport_options": [
                    option.model_dump() for option in research.transport_options
                ],
                "selected_transport": selected_transport.model_dump(),
                "budget": budget.model_dump(),
                "packing_list": packing,
            }
        )
        return DraftPlan.model_validate(values)

    @staticmethod
    def _deterministic_plan(
        request,
        research,
        budget,
        packing,
        selected_transport,
        scenario,
        over_budget_by,
        feedback,
    ) -> DraftPlan:
        time_slots = ["09:00", "11:30", "14:30", "18:30"]
        interests = request.interests
        attraction_names = []
        for attraction in research.attractions:
            name = re.split(r"\s+[—–-]\s+|\s*\(", attraction, maxsplit=1)[0].strip()
            if name and name not in attraction_names:
                attraction_names.append(name[:120])
        if not attraction_names:
            attraction_names = [f"a signature {interest} area" for interest in interests]
        preferences = " ".join(request.preferences).lower()
        activity_count = 3
        if any(value in preferences for value in ("slow", "relaxed", "low-key")):
            activity_count = 2
        elif any(value in preferences for value in ("fast", "packed", "busy")):
            activity_count = 4
        if scenario == "stretch" and activity_count > 2:
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
        remaining_activity_budget = round(
            budget.activities * (0.75 if scenario == "stretch" else 0.65),
            2,
        )
        daily_weights = [1 + ((index * 3) % 5) * 0.08 for index in range(request.days)]
        remaining_weight = sum(daily_weights)
        for index in range(request.days):
            interest = interests[index % len(interests)]
            attraction = attraction_names[index % len(attraction_names)]
            titles = [
                f"Explore {attraction}",
                f"Local lunch and walk near {attraction}",
                f"{interest.title()} experience in {request.destination}",
                f"Evening experience near {attraction}",
            ][:activity_count]
            if index == 0:
                titles[0] = "Arrival, check-in, and neighborhood orientation"
            if index == request.days - 1:
                titles[-1] = "Flexible closing activity and departure preparation"
            daily_budget = (
                remaining_activity_budget
                if index == request.days - 1
                else round(
                    remaining_activity_budget * daily_weights[index] / remaining_weight,
                    2,
                )
            )
            remaining_activity_budget = round(remaining_activity_budget - daily_budget, 2)
            remaining_weight -= daily_weights[index]
            cost_weights = [1.0, 1.35, 1.2, 0.8][: len(titles)]
            activity_costs = [
                round(daily_budget * weight / sum(cost_weights), 2)
                for weight in cost_weights
            ]
            activity_costs[-1] = round(daily_budget - sum(activity_costs[:-1]), 2)
            activities = [
                Activity(
                    time=time_slots[position],
                    title=title,
                    description=(
                        f"A research-informed fallback idea around {attraction} in "
                        f"{request.destination}; confirm the exact venue, price, hours, transit, "
                        "accessibility, and availability before booking."
                    ),
                    category=interest if position == 0 else "local experience",
                    estimated_cost=activity_costs[position],
                    source_url=None,
                )
                for position, title in enumerate(titles)
            ]
            days.append(
                ItineraryDay(
                    day=index + 1,
                    date=request.start_date + timedelta(days=index),
                    theme=f"{attraction}: {interest.title()} and local context",
                    activities=activities,
                    daily_notes=[
                        research.local_tips[index % len(research.local_tips)],
                        "Leave 30-45 minutes between major stops for local transit.",
                        *preference_notes,
                    ],
                    estimated_total=round(sum(item.estimated_cost for item in activities), 2),
                )
            )
        transport_assumption = (
            f"The sourced primary leg is estimated at "
            f"{selected_transport.priced_total_cost:.2f} {selected_transport.currency} "
            "for all travelers."
            if selected_transport.price_scope == "primary_leg_only"
            else f"The selected {selected_transport.mode} option is estimated at "
            f"{selected_transport.priced_total_cost:.2f} {selected_transport.currency} "
            "for all travelers."
        )
        assumptions = [
            transport_assumption,
            "Transport prices are search-derived estimates and must be verified before booking.",
            "The budget categories are spending allocations, not verified prices or quotes.",
            "Travel times and opening hours must be verified against final venues and dates.",
        ]
        if selected_transport.price_scope == "primary_leg_only":
            assumptions.append(
                "The transport total covers only the sourced primary leg. The onward connection "
                "must be priced separately and paid from the remaining trip allocation."
            )
        elif len(selected_transport.priced_legs) > 1:
            assumptions.append(
                f"The transport total combines {len(selected_transport.priced_legs)} sourced "
                f"route legs after converting each leg into {selected_transport.currency}."
            )
        if selected_transport.priced_total_in_destination_currency:
            converted = selected_transport.priced_total_in_destination_currency
            assumptions.append(
                f"The same estimated transport total is approximately {converted.amount:.2f} "
                f"{converted.currency} at a reference rate of {converted.exchange_rate:.6g}; "
                "the amount paid may differ."
            )
        if selected_transport.fare_basis == "assumed_per_person":
            assumptions.append(
                "The provider snippet did not explicitly label the fare per person. The plan "
                "uses that common listing assumption, which must be confirmed before booking."
            )
        if selected_transport.fare_date_basis == "dates_not_shown":
            assumptions.append(
                "The fare snippets do not display the requested dates. The search used those "
                "dates, but the prices must be checked for the exact itinerary."
            )
        elif selected_transport.fare_date_basis == "partial_dates":
            assumptions.append(
                "Only part of the requested date range is visible in the fare snippets. Confirm "
                "both departure and return dates before booking."
            )
        if selected_transport.connection_schedule_status == "not_verified":
            assumptions.append(
                "The separately sourced connection legs have not been schedule-matched. Allow "
                "time for delays, immigration, baggage collection, and airport-station transfer."
            )
        if selected_transport.round_trip_basis == "one_way_doubled":
            assumptions.append(
                "A provider explicitly showed a one-way fare, so that leg was doubled for a "
                "round-trip estimate. The actual return fare must be confirmed."
            )
        if feedback:
            assumptions.append(f"This revision addresses reviewer feedback: {feedback[:300]}")
        if request.preferences:
            assumptions.append(f"User preferences: {', '.join(request.preferences)}.")
        if scenario == "stretch":
            assumptions.append(
                f"This optional plan is {over_budget_by:.2f} {request.currency} above the user's "
                "maximum budget and uses the difference for enhanced destination spending."
            )
        lodging_intro = (
            "Use the enhanced lodging allocation for a central or higher-comfort stay."
            if scenario == "stretch"
            else "Choose a well-connected area that reduces daily transit."
        )
        return DraftPlan(
            scenario=scenario,
            over_budget_by=over_budget_by,
            requested_budget_min=request.budget_min,
            requested_budget_max=request.budget_max,
            title=(
                f"{request.days}-day {request.destination} "
                f"{scenario.replace('_', ' ')} itinerary"
            ),
            current_location=request.current_location,
            destination=request.destination,
            start_date=request.start_date,
            end_date=request.end_date,
            travelers=request.travelers,
            transport_summary=research.transport_summary,
            transport_options=research.transport_options,
            selected_transport=selected_transport,
            lodging_notes=[
                lodging_intro,
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
            avoided_places = list(change.avoid_places)
            if change.note:
                inferred_place = ItineraryPlannerAgent._place_to_avoid(change.note)
                if inferred_place:
                    avoided_places.append(inferred_place)
            avoided_places = list(
                dict.fromkeys(place.casefold() for place in avoided_places if place.strip())
            )
            if avoided_places:
                def contains_avoided_place(text, places=tuple(avoided_places)):
                    return any(place in str(text).casefold() for place in places)

                if contains_avoided_place(day["theme"]):
                    day["theme"] = "Alternative neighborhood and local experiences"
                day["daily_notes"] = [
                    note
                    for note in day["daily_notes"]
                    if not contains_avoided_place(note)
                ]
                day["daily_notes"].insert(
                    0,
                    "Keep this day's activities outside the location excluded by the reviewer.",
                )
                for activity in day["activities"]:
                    if not contains_avoided_place(
                        " ".join(
                            (
                                activity["title"],
                                activity["description"],
                                activity["category"],
                            )
                        )
                    ):
                        continue
                    activity["title"] = (
                        "Arrival, check-in, and alternative neighborhood orientation"
                        if "arrival" in activity["title"].casefold()
                        else "Meal in an alternative neighborhood"
                        if any(
                            word in activity["title"].casefold()
                            for word in ("food", "meal", "lunch", "dinner", "breakfast")
                        )
                        else "Alternative local experience"
                    )
                    activity["description"] = (
                        "Reviewer-requested alternative; select a different researched area and "
                        "verify its price, hours, transit, accessibility, and availability."
                    )
                    activity["category"] = "reviewer modification"
                    activity["source_url"] = None
            elif change.note:
                day["daily_notes"].insert(0, change.note)
        return DraftPlan.model_validate(values)

    @staticmethod
    def _place_to_avoid(instruction: str) -> str | None:
        patterns = (
            r"\bdo\s*not\s+want(?:\s+to\s+(?:visit|include|use))?\s+(.+)",
            r"\bdon'?t\s+want(?:\s+to\s+(?:visit|include|use))?\s+(.+)",
            r"\b(?:avoid|exclude|remove)\s+(.+)",
            r"\bwithout\s+(.+)",
        )
        for pattern in patterns:
            match = re.search(pattern, instruction, flags=re.IGNORECASE)
            if match is None:
                continue
            place = re.split(
                r"\b(?:on|for)\s+(?:this\s+day|day\s+\d+)\b",
                match.group(1),
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" .,!;:")
            return place[:120] or None
        return None
