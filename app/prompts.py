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
    "You are a careful destination research agent. Produce the requested schema, "
    "ground claims in the supplied results, keep source URLs unchanged, clearly "
    "label uncertainty, and never claim bookings or prices are confirmed. Preserve "
    "the supplied transport search results and budget-filtered transport options."
)

ITINERARY_SYSTEM_PROMPT = (
    "You are an itinerary planner. Return a feasible day-by-day plan matching "
    "every date exactly once, respect the budget, use realistic travel buffers, "
    "and do not claim reservations were made. Preserve the supplied current "
    "location and destination exactly in the output."
)


def build_research_prompt(
    request: TravelRequest,
    weather: WeatherSummary,
    search_results: list[SearchResult],
    transport_search_results: list[SearchResult],
    transport_options: list[TransportOption],
    feedback: str | None,
) -> str:
    prompt = (
        f"Travel request:\n{request.model_dump_json(indent=2)}\n\n"
        f"Weather context:\n{weather.model_dump_json(indent=2)}\n\n"
        "Web results:\n"
        + "\n".join(item.model_dump_json() for item in search_results)
        + "\n\nTransport search results:\n"
        + "\n".join(item.model_dump_json() for item in transport_search_results)
        + "\n\nBudget-filtered transport options, already sorted cheapest first:\n"
        + "\n".join(item.model_dump_json() for item in transport_options)
    )
    if feedback:
        prompt += f"\n\nReviewer feedback to address:\n{feedback}"
    return prompt


def build_itinerary_prompt(
    request: TravelRequest,
    research: ResearchReport,
    budget: BudgetBreakdown,
    packing: list[str],
    feedback: str | None,
    modifications: PlanModification | None,
) -> str:
    prompt = (
        f"Travel request:\n{request.model_dump_json(indent=2)}\n\n"
        f"Research report:\n{research.model_dump_json(indent=2)}\n\n"
        f"Budget tool output:\n{budget.model_dump_json(indent=2)}\n\n"
        f"Packing tool output:\n{packing}"
    )
    if feedback:
        prompt += f"\n\nReviewer feedback:\n{feedback}"
    if modifications:
        prompt += (
            "\n\nRequired modifications:\n"
            f"{modifications.model_dump_json(indent=2)}"
        )
    return prompt
