from typing import Any
from urllib.parse import quote_plus

import httpx

from app.config import Settings
from app.models import SearchResult, TravelRequest


class SearchConfigurationError(RuntimeError):
    pass


class WebSearchTool:
    SERPER_URL = "https://google.serper.dev/search"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds)

    def search(self, request: TravelRequest, limit: int = 8) -> list[SearchResult]:
        query = self._build_query(request)
        if self.settings.app_mode == "demo":
            return self._demo_results(request)
        if not self.settings.serper_api_key:
            raise SearchConfigurationError("Live mode requires SERPER_API_KEY.")
        return self._search_serper(query, limit)

    @staticmethod
    def _build_query(request: TravelRequest) -> str:
        interests = ", ".join(request.interests)
        return (
            f"{request.destination} travel guide attractions local tips safety weather "
            f"{request.start_date.isoformat()} {interests}"
        )

    def _search_serper(self, query: str, limit: int) -> list[SearchResult]:
        response = self.client.post(
            self.SERPER_URL,
            headers={"X-API-KEY": self.settings.serper_api_key or ""},
            json={"q": query, "num": limit},
        )
        response.raise_for_status()
        payload = response.json()
        results: list[SearchResult] = []
        for item in payload.get("organic", [])[:limit]:
            results.append(
                SearchResult(
                    title=item.get("title", "Untitled result"),
                    url=item.get("link"),
                    snippet=item.get("snippet", "No summary was returned."),
                    source="serper",
                )
            )
        return results

    @staticmethod
    def _demo_results(request: TravelRequest) -> list[SearchResult]:
        destination = request.destination
        encoded = quote_plus(destination)
        interest = request.interests[0]
        samples: list[dict[str, Any]] = [
            {
                "title": f"Visitor overview for {destination}",
                "url": f"https://en.wikipedia.org/wiki/Special:Search?search={encoded}",
                "snippet": (
                    f"Sample research overview covering major districts, signature sights, and "
                    f"visitor logistics in {destination}. Verify details before booking."
                ),
            },
            {
                "title": f"Local experiences and {interest}",
                "url": f"https://www.wikivoyage.org/wiki/en/index.php?search={encoded}",
                "snippet": (
                    f"Sample ideas for {interest}, local food, neighborhoods, and cultural "
                    f"etiquette. Live mode replaces this with current web results."
                ),
            },
            {
                "title": f"Travel safety notes for {destination}",
                "url": "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories.html/",
                "snippet": (
                    "Review official advisories, protect valuables, use licensed transport, and "
                    "confirm local emergency numbers before departure."
                ),
            },
        ]
        return [SearchResult(source="demo", **item) for item in samples]
