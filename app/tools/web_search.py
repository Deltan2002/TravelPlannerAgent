import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.cache import RedisCache
from app.config import Settings
from app.models import SearchResult, TransportOption, TravelRequest


class SearchConfigurationError(RuntimeError):
    pass


class WebSearchTool:
    SERPER_URL = "https://google.serper.dev/search"
    CURRENCY_MARKERS = {
        "USD": ("US$", "$"),
        "EUR": ("€",),
        "GBP": ("£",),
        "INR": ("₹", "Rs.", "Rs"),
        "JPY": ("JP¥", "¥"),
        "CAD": ("C$",),
        "AUD": ("A$",),
    }
    MODE_KEYWORDS = {
        "flight": ("flight", "airfare", "airline", "plane"),
        "train": ("train", "rail"),
        "bus": ("bus", "coach"),
        "ferry": ("ferry",),
        "car": ("car", "drive", "taxi"),
    }
    ROUND_TRIP_KEYWORDS = (
        "round trip",
        "round-trip",
        "roundtrip",
        "return fare",
        "return flight",
        "return train",
        "return bus",
        "return ferry",
    )
    PER_PERSON_KEYWORDS = ("per person", "per traveler", "per passenger", "each")

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        cache: RedisCache | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self.cache = cache or RedisCache(
            settings.redis_url, settings.cache_ttl_seconds, "search"
        )

    def search(self, request: TravelRequest, limit: int = 8) -> list[SearchResult]:
        query = self._build_query(request)
        if self.settings.app_mode == "demo":
            return self._demo_results(request)
        return self._cached_search(query, limit)

    def search_transport(
        self, request: TravelRequest, limit: int = 10
    ) -> tuple[list[SearchResult], list[TransportOption]]:
        if self.settings.app_mode == "demo" or request.origin_transport_budget == 0:
            return [], []
        query = self._build_transport_query(request)
        results = self._cached_search(query, limit)
        return results, self._extract_transport_options(request, results)

    def _cached_search(self, query: str, limit: int) -> list[SearchResult]:
        if not self.settings.serper_api_key:
            raise SearchConfigurationError("Live mode requires SERPER_API_KEY.")
        cache_key = f"{limit}:{query.casefold()}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, list):
            try:
                return [SearchResult.model_validate(item) for item in cached]
            except (TypeError, ValueError):
                pass
        results = self._search_serper(query, limit)
        self.cache.set(cache_key, [item.model_dump(mode="json") for item in results])
        return results

    @staticmethod
    def _build_query(request: TravelRequest) -> str:
        interests = ", ".join(request.interests)
        return (
            f"{request.destination} travel guide attractions local tips safety weather "
            f"{request.start_date.isoformat()} {interests} "
            f"traveling from {request.current_location}"
        )

    @staticmethod
    def _build_transport_query(request: TravelRequest) -> str:
        return (
            f"round trip transport from {request.current_location} to {request.destination} "
            f"depart {request.start_date.isoformat()} return {request.end_date.isoformat()} "
            f"flight train bus ferry car fare per person in {request.currency}"
        )

    def _extract_transport_options(
        self, request: TravelRequest, results: list[SearchResult]
    ) -> list[TransportOption]:
        options: list[TransportOption] = []
        seen: set[tuple[str, float, str]] = set()
        for result in results:
            if result.url is None:
                continue
            text = f"{result.title} {result.snippet}"
            if not self._matches_route(request, text):
                continue
            if not any(value in text.casefold() for value in self.ROUND_TRIP_KEYWORDS):
                continue
            if not any(value in text.casefold() for value in self.PER_PERSON_KEYWORDS):
                continue
            mode = self._transport_mode(text)
            price = self._price_in_currency(text, request.currency)
            if mode is None or price is None:
                continue
            per_person, evidence = price
            total = round(per_person * request.travelers, 2)
            if total > request.origin_transport_budget:
                continue
            identity = (mode, total, str(result.url))
            if identity in seen:
                continue
            seen.add(identity)
            options.append(
                TransportOption(
                    mode=mode,
                    source_title=result.title,
                    route=f"{request.current_location} to {request.destination}, round trip",
                    estimated_cost_per_person=per_person,
                    estimated_total_cost=total,
                    currency=request.currency,
                    travelers=request.travelers,
                    source_url=result.url,
                    price_evidence=evidence,
                )
            )
        return sorted(options, key=lambda option: (option.estimated_total_cost, option.mode))

    @staticmethod
    def _matches_route(request: TravelRequest, text: str) -> bool:
        normalized = text.casefold()
        origin = request.current_location.split(",", maxsplit=1)[0].strip().casefold()
        destination = request.destination.split(",", maxsplit=1)[0].strip().casefold()
        return origin in normalized and destination in normalized

    def _transport_mode(self, text: str) -> str | None:
        normalized = text.casefold()
        for mode, keywords in self.MODE_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords):
                return mode
        return None

    def _price_in_currency(self, text: str, currency: str) -> tuple[float, str] | None:
        number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
        markers = (currency, *self.CURRENCY_MARKERS.get(currency, ()))
        matches: list[tuple[float, str]] = []
        for marker in markers:
            escaped = re.escape(marker)
            prefix_boundary = r"\b" if marker == currency else r"(?<![A-Za-z])"
            patterns = [rf"{prefix_boundary}{escaped}\s*({number})"]
            if marker == currency:
                patterns.append(rf"({number})\s*{escaped}\b")
            for pattern in patterns:
                for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                    raw_amount = match.group(1).replace(",", "")
                    amount = float(raw_amount)
                    if amount > 0:
                        matches.append((amount, match.group(0)))
        return min(matches, key=lambda item: item[0]) if matches else None

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
                    f"visitor logistics in {destination} for a traveler starting in "
                    f"{request.current_location}. Verify details before booking."
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
