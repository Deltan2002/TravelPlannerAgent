from app.llm import StructuredLLM
from app.models import ResearchReport, TransportOption, TravelRequest
from app.prompts import RESEARCH_SYSTEM_PROMPT, build_research_prompt
from app.tools.destination_context import DestinationContextTool
from app.tools.web_search import WebSearchTool


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
            transport_search_results,
            transport_options,
            feedback,
        )
        try:
            generated = self.llm.generate(
                system_prompt=RESEARCH_SYSTEM_PROMPT,
                user_prompt=prompt,
                output_model=ResearchReport,
                schema_name="destination_research",
            )
        except ValueError:
            generated = None
        if generated is not None:
            return generated.model_copy(
                update={
                    "destination": request.destination,
                    "transport_summary": self._transport_summary(request, transport_options),
                    "transport_options": transport_options,
                    "transport_search_results": transport_search_results,
                    "weather": weather,
                    "search_results": search_results,
                }
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
                f"No sourced round-trip transport option was found at or below "
                f"{request.origin_transport_budget:.2f} {request.currency} for all travelers."
            )
        cheapest = options[0]
        return (
            f"Found {len(options)} sourced option(s) within the "
            f"{request.origin_transport_budget:.2f} {request.currency} allocation. "
            f"The lowest estimated total is {cheapest.estimated_total_cost:.2f} "
            f"{cheapest.currency} by {cheapest.mode}."
        )
