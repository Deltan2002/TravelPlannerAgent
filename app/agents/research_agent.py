import logging

from app.llm import StructuredLLM
from app.models import ResearchNarrative, ResearchReport, TransportOption, TravelRequest
from app.prompts import RESEARCH_SYSTEM_PROMPT, build_research_prompt
from app.tools.destination_context import DestinationContextTool
from app.tools.web_search import WebSearchTool

logger = logging.getLogger(__name__)


class ResearchAgent:
    def __init__(
        self,
        web_search: WebSearchTool,
        destination_context: DestinationContextTool,
        llm: StructuredLLM,
    ) -> None:
        self.web_search = web_search
        self.destination_context = destination_context
        self.llm = llm

    def run(self, request: TravelRequest, feedback: str | None = None) -> ResearchReport:
        search_results = self.web_search.search(request)
        transport_search_results, transport_options = self.web_search.search_transport(request)
        weather = self.destination_context.get_weather(request)
        prompt = build_research_prompt(
            request,
            weather,
            search_results,
            feedback,
        )
        try:
            generated = self.llm.generate(
                system_prompt=RESEARCH_SYSTEM_PROMPT,
                user_prompt=prompt,
                output_model=ResearchNarrative,
                schema_name="destination_research_narrative",
            )
        except (RuntimeError, ValueError) as exc:
            logger.info(
                "OpenAI research unavailable; continuing with deterministic fallback: %s",
                exc,
            )
            generated = None
        if generated is not None:
            return ResearchReport(
                destination=request.destination,
                **generated.model_dump(),
                transport_summary=self._transport_summary(request, transport_options),
                transport_options=transport_options,
                transport_search_results=transport_search_results,
                weather=weather,
                search_results=search_results,
            )
        return self._deterministic_report(
            request,
            search_results,
            transport_search_results,
            transport_options,
            weather,
            feedback,
        )

    @staticmethod
    def _deterministic_report(
        request,
        search_results,
        transport_search_results,
        transport_options,
        weather,
        feedback,
    ) -> ResearchReport:
        destination = request.destination
        attractions = [
            f"A landmark-focused orientation walk in {destination}",
            *[
                f"A neighborhood experience connected to {interest}"
                for interest in request.interests[:3]
            ],
            f"A local market or food district in {destination}",
        ]
        safety = [
            "Check official travel advisories and local emergency contacts before departure.",
            "Use licensed transport and keep valuables secured in crowded visitor areas.",
        ]
        if feedback:
            safety.append(f"Revision research was requested with this focus: {feedback[:240]}")
        preference_context = (
            f" Preferences to account for: {', '.join(request.preferences)}."
            if request.preferences
            else ""
        )
        return ResearchReport(
            destination=destination,
            summary=(
                f"A planning brief for travel from {request.current_location} to {destination}, "
                "focused on "
                f"{', '.join(request.interests)}. Sources are discovery inputs; operating hours, "
                "availability, advisories, and prices must be reconfirmed before booking."
                f"{preference_context}"
            ),
            attractions=attractions,
            local_tips=[
                "Group nearby activities to reduce transit time.",
                "Reserve high-demand attractions and restaurants when dates are confirmed.",
                "Keep one flexible period for weather or energy-level changes.",
            ],
            safety_notes=safety,
            season_notes=[weather.summary],
            transport_summary=ResearchAgent._transport_summary(request, transport_options),
            transport_options=transport_options,
            transport_search_results=transport_search_results,
            weather=weather,
            search_results=search_results,
        )

    @staticmethod
    def _transport_summary(
        request: TravelRequest, options: list[TransportOption]
    ) -> str:
        if not options:
            return (
                f"No verified round-trip transport option was found from "
                f"{request.current_location} to {request.destination}."
            )
        cheapest = options[0]
        date_note = {
            "exact_dates": "Both requested dates appear in the selected fare evidence.",
            "partial_dates": "Only part of the requested date range appears in the fare evidence.",
            "dates_not_shown": "The fare snippets do not display the requested dates.",
        }[cheapest.fare_date_basis]
        if cheapest.price_scope == "primary_leg_only":
            return (
                f"Found {len(options)} connected transport option(s), sorted by the sourced "
                f"primary-leg cost. The lowest sourced portion is "
                f"{cheapest.priced_total_cost:.2f} {cheapest.currency}. "
                f"{cheapest.connection_note} {date_note}"
            )
        if cheapest.mode == "multimodal":
            return (
                f"Found {len(options)} transport option(s), sorted by the complete estimated "
                f"round-trip cost. The lowest connected route has "
                f"{len(cheapest.priced_legs)} priced legs totaling "
                f"{cheapest.priced_total_cost:.2f} {cheapest.currency} for all travelers. "
                f"The separate legs are not schedule-matched. {date_note}"
            )
        fare_note = (
            " The provider snippet did not explicitly say per person, so the displayed fare is "
            "treated as a per-person estimate and must be verified."
            if cheapest.fare_basis == "assumed_per_person"
            else ""
        )
        return (
            f"Found {len(options)} transport option(s), sorted by estimated total cost. "
            f"The lowest estimated total is {cheapest.priced_total_cost:.2f} "
            f"{cheapest.currency} by {cheapest.mode}. {date_note}{fare_note}"
        )
