import json

from app.models import (
    BudgetBreakdown,
    PlanModification,
    ResearchReport,
    SearchResult,
    TransportOption,
    TravelRequest,
    WeatherSummary,
)

RESEARCH_SYSTEM_PROMPT = (
    "You are a careful destination research agent. Write only the concise narrative "
    "fields requested by the schema. Ground claims in the supplied context, clearly "
    "label uncertainty, and never claim bookings, opening hours, or prices are confirmed."
)

ITINERARY_SYSTEM_PROMPT = (
    "You are an itinerary planner. Write only the title, lodging notes, day-by-day "
    "itinerary, and assumptions requested by the schema. Match every trip date exactly "
    "once, keep activity costs within the activity allocation, use realistic travel "
    "buffers, and do not claim reservations were made."
)


def build_research_prompt(
    request: TravelRequest,
    weather: WeatherSummary,
    search_results: list[SearchResult],
    feedback: str | None,
) -> str:
    context = {
        "travel_request": request.model_dump(mode="json"),
        "display_date_format": "DD/MM/YYYY in human-readable text",
        "weather": weather.model_dump(mode="json"),
        "web_results": [item.model_dump(mode="json") for item in search_results],
    }
    prompt = (
        "Create concise destination research from this JSON context:\n"
        + json.dumps(context, separators=(",", ":"))
    )
    if feedback:
        prompt += f"\n\nReviewer feedback to address:\n{feedback}"
    return prompt


def build_itinerary_prompt(
    request: TravelRequest,
    research: ResearchReport,
    budget: BudgetBreakdown,
    feedback: str | None,
    modifications: PlanModification | None,
    scenario: str,
    over_budget_by: float,
    selected_transport: TransportOption,
) -> str:
    context = {
        "travel_request": request.model_dump(mode="json"),
        "research": {
            "summary": research.summary,
            "attractions": research.attractions,
            "local_tips": research.local_tips,
            "safety_notes": research.safety_notes,
            "season_notes": research.season_notes,
            "sources": [
                {
                    "title": result.title,
                    "url": str(result.url),
                    "snippet": result.snippet,
                }
                for result in research.search_results
                if result.url
            ],
        },
        "scenario": scenario,
        "scenario_instructions": (
            "Create a distinct enhanced itinerary that uses the larger allocation for more "
            "varied destination-specific experiences, better timing, comfort, or worthwhile "
            "day trips. Do not copy the within-budget itinerary or fill days with generic "
            "placeholder activities. Keep individual costs realistic rather than spending the "
            "full allocation artificially."
            if scenario == "stretch"
            else "Create a practical cost-conscious itinerary with destination-specific places, "
            "realistic timing, and sensible daily travel."
        ),
        "display_date_format": "DD/MM/YYYY in human-readable text",
        "amount_over_maximum_budget": over_budget_by,
        "selected_transport": {
            "mode": selected_transport.mode,
            "route": selected_transport.route,
            "route_legs": selected_transport.route_legs,
            "price_scope": selected_transport.price_scope,
            "priced_total_cost": selected_transport.priced_total_cost,
            "currency": selected_transport.currency,
            "connection_note": selected_transport.connection_note,
        },
        "budget": budget.model_dump(mode="json"),
    }
    prompt = (
        "Create the itinerary content from this JSON context:\n"
        + json.dumps(context, separators=(",", ":"))
    )
    if feedback:
        prompt += f"\n\nReviewer feedback:\n{feedback}"
    if modifications:
        prompt += (
            "\n\nRequired modifications:\n"
            f"{modifications.model_dump_json(indent=2)}"
        )
    return prompt
