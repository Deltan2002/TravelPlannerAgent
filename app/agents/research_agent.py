from app.llm import StructuredLLM
from app.models import ResearchReport, TravelRequest
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
        weather = self.destination_context.get_weather(request)
        prompt = (
            f"Travel request:\n{request.model_dump_json(indent=2)}\n\n"
            f"Weather context:\n{weather.model_dump_json(indent=2)}\n\n"
            "Web results:\n"
            + "\n".join(item.model_dump_json() for item in search_results)
        )
        if feedback:
            prompt += f"\n\nReviewer feedback to address:\n{feedback}"
        try:
            generated = self.llm.generate(
                system_prompt=(
                    "You are a careful destination research agent. Produce the requested schema, "
                    "ground claims in the supplied results, keep source URLs unchanged, clearly "
                    "label uncertainty, and never claim bookings or prices are confirmed."
                ),
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
                    "weather": weather,
                    "search_results": search_results,
                }
            )
        return self._deterministic_report(request, search_results, weather, feedback)

    @staticmethod
    def _deterministic_report(request, search_results, weather, feedback) -> ResearchReport:
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
            weather=weather,
            search_results=search_results,
        )
