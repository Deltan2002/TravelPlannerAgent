import re
from statistics import median
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from app.cache import RedisCache
from app.config import Settings
from app.models import (
    CabinClass,
    FareDateBasis,
    SearchResult,
    TransportLeg,
    TransportMode,
    TransportOption,
    TravelRequest,
)
from app.tools.currency import CurrencyConverterTool


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
        "flight": ("flight", "flights", "airfare", "airline", "airlines", "plane"),
        "train": ("train", "trains", "rail"),
        "bus": ("bus", "buses", "coach"),
        "ferry": ("ferry", "ferries"),
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
    PER_PERSON_KEYWORDS = (
        "per person",
        "per traveler",
        "per passenger",
        "one adult",
        "adult fare",
        "each",
    )
    ONE_WAY_KEYWORDS = ("one way", "one-way", "single fare", "single ticket")
    MAX_FARE_LABEL_DISTANCE = 55
    CABIN_KEYWORDS = {
        "premium_economy": ("premium economy", "premium-economy"),
        "business": ("business class", "business-class"),
        "first": ("first class", "first-class"),
        "economy": (
            "economy class",
            "economy-class",
            "economy fare",
            "coach class",
            "passenger, economy",
            "passenger economy",
            "economy",
        ),
    }
    NON_FARE_PHRASES = (
        "per km",
        "per kilometer",
        "ic card",
        "prepaid card",
        "this card",
        "deposit",
        "land and air package",
        "land & air package",
    )
    BLOCKED_TRANSPORT_HOSTS = (
        "facebook.com",
        "reddit.com",
        "quora.com",
    )
    LOCATION_ALIAS_GROUPS = (
        ("bengaluru", "bangalore", "blr"),
        ("mumbai", "bombay", "bom"),
        ("new delhi", "delhi", "del"),
        ("chennai", "madras", "maa"),
        ("kolkata", "calcutta", "ccu"),
    )
    DESTINATION_GATEWAYS = {
        "kyoto": {"tokyo", "osaka", "kansai", "kix", "nrt", "hnd"},
        "bonn": {
            "frankfurt",
            "fra",
            "cologne",
            "koln",
            "köln",
            "cgn",
            "dusseldorf",
            "düsseldorf",
            "dus",
        },
    }
    MONTHS = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        cache: RedisCache | None = None,
        currency_converter: CurrencyConverterTool | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self.cache = cache or RedisCache(
            settings.redis_url, settings.cache_ttl_seconds, "search"
        )
        self.currency_converter = currency_converter or CurrencyConverterTool(
            settings, self.client
        )

    def search(self, request: TravelRequest, limit: int = 8) -> list[SearchResult]:
        query = self._build_query(request)
        if self.settings.app_mode == "demo":
            return self._demo_results(request)
        return [
            result
            for result in self._cached_search(query, limit)
            if result.url is None or not self._blocked_transport_source(str(result.url))
        ]

    def search_transport(
        self, request: TravelRequest, limit: int = 10
    ) -> tuple[list[SearchResult], list[TransportOption]]:
        if self.settings.app_mode == "demo":
            results = self._demo_transport_results(request)
            options = self._extract_transport_options(request, results)
            return results, [self._add_primary_leg_details(request, option) for option in options]
        query = self._build_transport_query(request)
        results = self._cached_search(query, limit)
        options = self._extract_transport_options(request, results)
        all_results = list(results)
        enriched: list[TransportOption] = []
        for option in options:
            if option.price_scope == "primary_leg_only" and option.gateway:
                connection_mode = self._connection_mode(request)
                destination_currency = (
                    self.currency_converter.destination_currency(request.destination)
                    or request.currency
                )
                connection_query = self._build_connection_query(
                    request,
                    option.gateway,
                    destination_currency,
                )
                try:
                    connection_results = self._cached_search(connection_query, 8)
                except httpx.HTTPError:
                    connection_results = []
                all_results.extend(connection_results)
                connection_leg = self._extract_connection_leg(
                    request,
                    option.gateway,
                    connection_mode,
                    destination_currency,
                    connection_results,
                )
                if connection_leg is not None:
                    enriched.append(
                        self._complete_connected_option(request, option, connection_leg)
                    )
                    continue
            enriched.append(self._add_primary_leg_details(request, option))
        return all_results, sorted(
            enriched,
            key=lambda option: (option.priced_total_cost, option.mode),
        )

    @staticmethod
    def _build_connection_query(
        request: TravelRequest,
        gateway: str,
        currency: str,
    ) -> str:
        modes = " ".join(
            mode for mode in request.transport_modes if mode != "flight"
        )
        return (
            f"round trip {modes} from {gateway} to {request.destination} fare per person "
            f"in {currency} depart {request.start_date.isoformat()} "
            f"return {request.end_date.isoformat()}"
        )

    def _extract_connection_leg(
        self,
        request: TravelRequest,
        gateway: str,
        preferred_mode: TransportMode | None,
        local_currency: str,
        results: list[SearchResult],
    ) -> TransportLeg | None:
        candidates: list[TransportLeg] = []
        allowed_modes = {
            mode for mode in request.transport_modes if mode != "flight"
        }
        if preferred_mode:
            allowed_modes.add(preferred_mode)
        for result in results:
            if result.url is None or self._blocked_transport_source(str(result.url)):
                continue
            text = f"{result.title} {result.snippet} {result.url}"
            if not self._matches_location(gateway, text) or not self._matches_location(
                request.destination, text
            ):
                continue
            mode = self._transport_mode(text)
            if mode is None or mode not in allowed_modes:
                continue
            for quote in self._fare_quotes(request, result, mode, local_currency):
                (
                    per_person,
                    price_currency,
                    evidence,
                    cabin,
                    fare_basis,
                    trip_basis,
                    date_basis,
                ) = quote
                source_total = round(per_person * request.travelers, 2)
                converted = self.currency_converter.convert(
                    source_total,
                    price_currency,
                    request.currency,
                )
                if converted is None:
                    continue
                candidates.append(
                    TransportLeg(
                        mode=mode,
                        cabin_class=cabin,
                        fare_date_basis=date_basis,
                        origin=gateway,
                        destination=request.destination,
                        estimated_cost_per_person=per_person,
                        estimated_total_cost=source_total,
                        currency=price_currency,
                        travelers=request.travelers,
                        estimated_total_in_budget_currency=converted.amount,
                        budget_currency=request.currency,
                        fare_basis=fare_basis,
                        round_trip_basis=trip_basis,
                        source_title=result.title,
                        source_url=result.url,
                        price_evidence=evidence,
                    )
                )
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda leg: leg.estimated_total_in_budget_currency,
        )

    def _add_primary_leg_details(
        self,
        request: TravelRequest,
        option: TransportOption,
    ) -> TransportOption:
        if option.priced_legs:
            primary_leg = option.priced_legs[0]
        else:
            primary_mode: TransportMode = (
                "flight" if option.mode == "multimodal" else option.mode
            )
            primary_leg = TransportLeg(
                mode=primary_mode,
                cabin_class=option.cabin_class,
                fare_date_basis=option.fare_date_basis,
                origin=request.current_location,
                destination=option.gateway or request.destination,
                estimated_cost_per_person=option.priced_cost_per_person,
                estimated_total_cost=option.priced_total_cost,
                currency=option.currency,
                travelers=option.travelers,
                estimated_total_in_budget_currency=option.priced_total_cost,
                budget_currency=request.currency,
                fare_basis=option.fare_basis,
                round_trip_basis=option.round_trip_basis,
                source_title=option.source_title,
                source_url=option.source_url,
                price_evidence=option.price_evidence,
            )
        destination_currency = self.currency_converter.destination_currency(
            request.destination
        )
        converted_total = (
            self.currency_converter.convert(
                option.priced_total_cost,
                request.currency,
                destination_currency,
            )
            if destination_currency
            else None
        )
        values = option.model_dump()
        values.update(
            {
                "priced_legs": [primary_leg.model_dump()],
                "priced_total_in_destination_currency": (
                    converted_total.model_dump() if converted_total else None
                ),
            }
        )
        return TransportOption.model_validate(values)

    def _complete_connected_option(
        self,
        request: TravelRequest,
        option: TransportOption,
        connection_leg: TransportLeg,
    ) -> TransportOption:
        primary = self._add_primary_leg_details(request, option).priced_legs[0]
        legs = [primary, connection_leg]
        total = round(
            sum(leg.estimated_total_in_budget_currency for leg in legs),
            2,
        )
        destination_currency = self.currency_converter.destination_currency(
            request.destination
        )
        converted_total = (
            self.currency_converter.convert(total, request.currency, destination_currency)
            if destination_currency
            else None
        )
        values = option.model_dump()
        values.update(
            {
                "price_scope": "complete_route",
                "fare_basis": (
                    "assumed_per_person"
                    if any(leg.fare_basis == "assumed_per_person" for leg in legs)
                    else "explicit_per_person"
                ),
                "round_trip_basis": (
                    "one_way_doubled"
                    if any(leg.round_trip_basis == "one_way_doubled" for leg in legs)
                    else "explicit_round_trip"
                ),
                "fare_date_basis": (
                    "exact_dates"
                    if all(leg.fare_date_basis == "exact_dates" for leg in legs)
                    else "partial_dates"
                    if any(leg.fare_date_basis != "dates_not_shown" for leg in legs)
                    else "dates_not_shown"
                ),
                "connection_schedule_status": "not_verified",
                "connection_note": (
                    "Both route legs have sourced estimates, but they are separate results. "
                    "Verify availability, fare rules, airport-to-station time, and the transfer "
                    "schedule before booking."
                ),
                "priced_cost_per_person": round(total / request.travelers, 2),
                "priced_total_cost": total,
                "price_evidence": (
                    f"Primary leg: {primary.price_evidence}; connection: "
                    f"{connection_leg.price_evidence}"
                ),
                "priced_legs": [leg.model_dump() for leg in legs],
                "unpriced_legs": [],
                "priced_total_in_destination_currency": (
                    converted_total.model_dump() if converted_total else None
                ),
            }
        )
        return TransportOption.model_validate(values)

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
        modes = " ".join(request.transport_modes)
        cabin = (
            "economy premium economy business class first class"
            if request.include_premium_fares
            else "economy class"
        )
        connections = (
            "including a flight to a major gateway and onward ground connection"
            if request.allow_transport_connections
            else "direct route only"
        )
        return (
            f"round trip transport from {request.current_location} to {request.destination} "
            f"depart {request.start_date.isoformat()} return {request.end_date.isoformat()} "
            f"{modes} {cabin} fare per person in {request.currency} {connections}"
        )

    def _extract_transport_options(
        self, request: TravelRequest, results: list[SearchResult]
    ) -> list[TransportOption]:
        direct_options: list[TransportOption] = []
        connected_options: list[TransportOption] = []
        seen: set[tuple[str, float, str, str]] = set()
        for result in results:
            if result.url is None or self._blocked_transport_source(str(result.url)):
                continue
            text = f"{result.title} {result.snippet} {result.url}"
            mode = self._transport_mode(text)
            if mode is None or mode not in request.transport_modes:
                continue
            quotes = self._fare_quotes(request, result, mode)
            if not quotes:
                continue
            if self._matches_route(request, text):
                for quote in quotes:
                    option = self._direct_option(request, result, mode, quote)
                    if option is None:
                        continue
                    identity = (
                        mode,
                        option.priced_total_cost,
                        str(result.url),
                        option.cabin_class,
                    )
                    if identity not in seen:
                        seen.add(identity)
                        direct_options.append(option)
                continue
            gateway = self._gateway_name(request, result.title, text)
            connection_mode = self._connection_mode(request)
            if (
                not request.allow_transport_connections
                or mode != "flight"
                or gateway is None
                or connection_mode is None
            ):
                continue
            for quote in quotes:
                option = self._connected_option(
                    request,
                    result,
                    gateway,
                    connection_mode,
                    quote,
                )
                if option is None:
                    continue
                identity = (
                    "multimodal",
                    option.priced_total_cost,
                    str(result.url),
                    option.cabin_class,
                )
                if identity not in seen:
                    seen.add(identity)
                    connected_options.append(option)
        options = self._remove_fare_outliers(direct_options or connected_options)
        return sorted(options, key=lambda option: (option.priced_total_cost, option.mode))

    def _direct_option(
        self,
        request: TravelRequest,
        result: SearchResult,
        mode: TransportMode,
        quote: tuple[float, str, str, CabinClass, str, str, FareDateBasis],
    ) -> TransportOption | None:
        (
            source_per_person,
            source_currency,
            evidence,
            cabin,
            fare_basis,
            trip_basis,
            date_basis,
        ) = quote
        source_total = round(source_per_person * request.travelers, 2)
        converted = self.currency_converter.convert(
            source_total,
            source_currency,
            request.currency,
        )
        if converted is None:
            return None
        total = converted.amount
        leg = TransportLeg(
            mode=mode,
            cabin_class=cabin,
            fare_date_basis=date_basis,
            origin=request.current_location,
            destination=request.destination,
            estimated_cost_per_person=source_per_person,
            estimated_total_cost=source_total,
            currency=source_currency,
            travelers=request.travelers,
            estimated_total_in_budget_currency=total,
            budget_currency=request.currency,
            fare_basis=fare_basis,
            round_trip_basis=trip_basis,
            source_title=result.title,
            source_url=result.url,
            price_evidence=evidence,
        )
        return TransportOption(
            mode=mode,
            cabin_class=cabin,
            fare_date_basis=date_basis,
            source_title=result.title,
            route=f"{request.current_location} to {request.destination}, round trip",
            route_legs=[
                f"{mode.title()}: {request.current_location} to "
                f"{request.destination}, round trip"
            ],
            fare_basis=fare_basis,
            round_trip_basis=trip_basis,
            priced_cost_per_person=round(total / request.travelers, 2),
            priced_total_cost=total,
            currency=request.currency,
            travelers=request.travelers,
            source_url=result.url,
            price_evidence=evidence,
            priced_legs=[leg],
        )

    def _connected_option(
        self,
        request: TravelRequest,
        result: SearchResult,
        gateway: str,
        connection_mode: TransportMode,
        quote: tuple[float, str, str, CabinClass, str, str, FareDateBasis],
    ) -> TransportOption | None:
        (
            source_per_person,
            source_currency,
            evidence,
            cabin,
            fare_basis,
            trip_basis,
            date_basis,
        ) = quote
        source_total = round(source_per_person * request.travelers, 2)
        converted = self.currency_converter.convert(
            source_total,
            source_currency,
            request.currency,
        )
        if converted is None:
            return None
        total = converted.amount
        primary_leg = TransportLeg(
            mode="flight",
            cabin_class=cabin,
            fare_date_basis=date_basis,
            origin=request.current_location,
            destination=gateway,
            estimated_cost_per_person=source_per_person,
            estimated_total_cost=source_total,
            currency=source_currency,
            travelers=request.travelers,
            estimated_total_in_budget_currency=total,
            budget_currency=request.currency,
            fare_basis=fare_basis,
            round_trip_basis=trip_basis,
            source_title=result.title,
            source_url=result.url,
            price_evidence=evidence,
        )
        return TransportOption(
            mode="multimodal",
            cabin_class=cabin,
            fare_date_basis=date_basis,
            connection_schedule_status="not_verified",
            source_title=result.title,
            route=(
                f"{request.current_location} to {gateway} by flight, then {gateway} to "
                f"{request.destination} by {connection_mode}, round trip"
            ),
            gateway=gateway,
            route_legs=[
                f"Flight: {request.current_location} to {gateway}, round trip",
                f"{connection_mode.title()}: {gateway} to {request.destination}, "
                "outbound and return connection",
            ],
            price_scope="primary_leg_only",
            fare_basis=fare_basis,
            round_trip_basis=trip_basis,
            connection_note=(
                f"The sourced price covers the {request.current_location} to {gateway} flight. "
                f"The {gateway} to {request.destination} {connection_mode} must be priced "
                "separately before the route is complete."
            ),
            priced_cost_per_person=round(total / request.travelers, 2),
            priced_total_cost=total,
            currency=request.currency,
            travelers=request.travelers,
            source_url=result.url,
            price_evidence=evidence,
            priced_legs=[primary_leg],
            unpriced_legs=[
                {
                    "mode": connection_mode,
                    "origin": gateway,
                    "destination": request.destination,
                    "reason": (
                        "No verified fare with a clear trip-price basis was found for this leg."
                    ),
                }
            ],
        )

    @classmethod
    def _matches_route(cls, request: TravelRequest, text: str) -> bool:
        return cls._matches_location(request.current_location, text) and cls._matches_location(
            request.destination, text
        )

    @classmethod
    def _matches_location(cls, location: str, text: str) -> bool:
        normalized = text.casefold()
        return any(
            re.search(rf"\b{re.escape(value)}\b", normalized)
            for value in cls._location_variants(location)
        )

    @classmethod
    def _location_variants(cls, location: str) -> set[str]:
        city = location.split(",", maxsplit=1)[0].strip().casefold()
        variants = {city}
        for group in cls.LOCATION_ALIAS_GROUPS:
            if city in group:
                variants.update(group)
        return variants

    @classmethod
    def _gateway_name(
        cls, request: TravelRequest, title: str, combined_text: str
    ) -> str | None:
        if not cls._matches_location(request.current_location, combined_text):
            return None
        match = re.search(
            r"\bto\s+([A-Za-z][A-Za-z .'-]{1,40}?)(?=\s*(?:\(|[-–|,:]|flights?\b|fares?\b|on\b|$))",
            title,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        gateway = match.group(1).strip(" .'-")
        gateway = re.sub(
            r"\s+(?:cheap|direct|nonstop|non-stop)$",
            "",
            gateway,
            flags=re.IGNORECASE,
        )
        destination_country = request.destination.rsplit(",", maxsplit=1)[-1].strip()
        destination_city = request.destination.split(",", maxsplit=1)[0].strip().casefold()
        known_gateways = cls.DESTINATION_GATEWAYS.get(destination_city, set())
        country_matches = destination_country.casefold() in combined_text.casefold()
        if not country_matches and gateway.casefold() not in known_gateways:
            return None
        if not gateway or gateway.casefold() == destination_country.casefold():
            return None
        if gateway.casefold() in cls._location_variants(request.destination):
            return None
        return gateway

    @staticmethod
    def _connection_mode(request: TravelRequest) -> TransportMode | None:
        for mode in ("train", "bus", "car", "ferry"):
            if mode in request.transport_modes:
                return mode
        return None

    def _transport_mode(self, text: str) -> str | None:
        normalized = text.casefold()
        for mode, keywords in self.MODE_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords):
                return mode
        return None

    def _fare_quotes(
        self,
        request: TravelRequest,
        result: SearchResult,
        mode: TransportMode,
        preferred_currency: str | None = None,
    ) -> list[tuple[float, str, str, CabinClass, str, str, FareDateBasis]]:
        date_basis = self._fare_date_basis(request, result.snippet)
        if date_basis is None:
            return []
        destination_currency = self.currency_converter.destination_currency(
            request.destination
        )
        currencies = list(
            dict.fromkeys(
                value
                for value in (
                    preferred_currency,
                    request.currency,
                    destination_currency,
                    *self.CURRENCY_MARKERS,
                    "CHF",
                    "SGD",
                    "THB",
                    "AED",
                )
                if value
            )
        )
        quotes: list[
            tuple[float, str, str, CabinClass, str, str, FareDateBasis]
        ] = []
        for text in (result.snippet, result.title):
            text_quotes: list[
                tuple[float, str, str, CabinClass, str, str, FareDateBasis]
            ] = []
            for currency in currencies:
                for amount, evidence, start, end in self._prices_in_currency(text, currency):
                    context = text[max(0, start - 120) : min(len(text), end + 120)]
                    normalized = context.casefold()
                    if any(phrase in normalized for phrase in self.NON_FARE_PHRASES):
                        continue
                    cabin = self._cabin_class(text, start, end, mode)
                    if (
                        mode == "flight"
                        and cabin in {"premium_economy", "business", "first"}
                        and not request.include_premium_fares
                    ):
                        continue
                    trip_basis = self._trip_basis(text, start, end)
                    if trip_basis is None:
                        continue
                    fare_basis = (
                        "explicit_per_person"
                        if self._explicit_per_person(text, start, end)
                        else "assumed_per_person"
                    )
                    if trip_basis == "one_way_doubled":
                        amount = round(amount * 2, 2)
                        evidence = f"{evidence} one-way fare; doubled for a round trip"
                    text_quotes.append(
                        (
                            amount,
                            currency,
                            evidence,
                            cabin,
                            fare_basis,
                            trip_basis,
                            date_basis,
                        )
                    )
            if text_quotes:
                quotes.extend(text_quotes)
                break
        unique = list(dict.fromkeys(quotes))
        explicit_round_trips = [
            quote for quote in unique if quote[5] == "explicit_round_trip"
        ]
        if explicit_round_trips:
            unique = explicit_round_trips
        cabin_priority = {
            "economy": 0,
            "unspecified": 1,
            "premium_economy": 2,
            "business": 3,
            "first": 4,
        }
        return sorted(
            unique,
            key=lambda quote: (
                quote[1] != (preferred_currency or request.currency),
                cabin_priority[quote[3]],
                quote[0],
            ),
        )

    @classmethod
    def _fare_date_basis(
        cls,
        request: TravelRequest,
        text: str,
    ) -> FareDateBasis | None:
        found: list[tuple[int, int, int | None]] = []
        for value in re.findall(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
            found.append((int(value[1]), int(value[2]), int(value[0])))
        month_names = "|".join(sorted(cls.MONTHS, key=len, reverse=True))
        for match in re.finditer(
            rf"\b({month_names})\s+(\d{{1,2}})(?:,?\s+(20\d{{2}}))?\b",
            text,
            flags=re.IGNORECASE,
        ):
            found.append(
                (
                    cls.MONTHS[match.group(1).casefold()],
                    int(match.group(2)),
                    int(match.group(3)) if match.group(3) else None,
                )
            )
        for match in re.finditer(
            rf"\b(\d{{1,2}})\s+({month_names})(?:\s+(20\d{{2}}))?\b",
            text,
            flags=re.IGNORECASE,
        ):
            found.append(
                (
                    cls.MONTHS[match.group(2).casefold()],
                    int(match.group(1)),
                    int(match.group(3)) if match.group(3) else None,
                )
            )
        if not found:
            return "dates_not_shown"
        expected = {
            (request.start_date.month, request.start_date.day, request.start_date.year),
            (request.end_date.month, request.end_date.day, request.end_date.year),
        }
        matched: set[tuple[int, int, int]] = set()
        for month, day, year in found:
            candidates = {
                value
                for value in expected
                if value[0] == month and value[1] == day and (year is None or value[2] == year)
            }
            if not candidates:
                return None
            matched.update(candidates)
        return "exact_dates" if matched == expected else "partial_dates"

    @classmethod
    def _explicit_per_person(
        cls,
        text: str,
        start: int,
        end: int,
    ) -> bool:
        if cls._near_keyword(text, start, end, cls.PER_PERSON_KEYWORDS, 100):
            return True
        context = text[max(0, start - 100) : min(len(text), end + 100)]
        return bool(
            re.search(
                r"\b(?:\d+|one)\s+(?:adult|passenger|traveler|traveller)s?\b",
                context,
                flags=re.IGNORECASE,
            )
        )

    def _prices_in_currency(
        self,
        text: str,
        currency: str,
    ) -> list[tuple[float, str, int, int]]:
        number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
        markers = (currency, *self.CURRENCY_MARKERS.get(currency, ()))
        matches: list[tuple[float, str, int, int]] = []
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
                        matches.append(
                            (amount, match.group(0), match.start(), match.end())
                        )
        return list(dict.fromkeys(matches))

    @classmethod
    def _cabin_class(
        cls,
        text: str,
        start: int,
        end: int,
        mode: TransportMode,
    ) -> CabinClass:
        if mode != "flight":
            return "unspecified"
        matches: list[tuple[int, CabinClass]] = []
        for cabin, keywords in cls.CABIN_KEYWORDS.items():
            distance = cls._keyword_distance(text, start, end, keywords)
            if distance is not None and distance <= 100:
                matches.append((distance, cabin))
        if not matches:
            return "unspecified"
        matches.sort()
        nearest = matches[0]
        if nearest[1] == "economy":
            premium = next(
                (
                    candidate
                    for candidate in matches
                    if candidate[1] == "premium_economy"
                    and candidate[0] <= nearest[0] + 10
                ),
                None,
            )
            if premium:
                return premium[1]
        return nearest[1]

    @classmethod
    def _trip_basis(
        cls,
        text: str,
        start: int,
        end: int,
    ) -> str | None:
        clause_start = max(
            (text.rfind(separator, 0, start) for separator in (".", ";", "|", "•", "\n")),
            default=-1,
        ) + 1
        clause_ends = [
            position
            for separator in (".", ";", "|", "•", "\n")
            if (position := text.find(separator, end)) >= 0
        ]
        clause_end = min(clause_ends, default=len(text))
        text = text[clause_start:clause_end]
        start -= clause_start
        end -= clause_start
        following = text[end : min(len(text), end + 45)]
        following_label = re.match(
            r"^\s*(?:per\s+(?:person|traveler|traveller|passenger)\s*)?"
            r"(?:for\s+)?(?:a\s+)?"
            r"(round[ -]?trip|return fare|one[ -]?way|single fare|single ticket)\b",
            following,
            flags=re.IGNORECASE,
        )
        if following_label:
            label = following_label.group(1).casefold()
            return (
                "one_way_doubled"
                if "one" in label or "single" in label
                else "explicit_round_trip"
            )
        round_trip_distance = cls._keyword_distance(
            text,
            start,
            end,
            cls.ROUND_TRIP_KEYWORDS,
        )
        one_way_distance = cls._keyword_distance(
            text,
            start,
            end,
            cls.ONE_WAY_KEYWORDS,
        )
        candidates = [
            (distance, basis)
            for distance, basis in (
                (round_trip_distance, "explicit_round_trip"),
                (one_way_distance, "one_way_doubled"),
            )
            if distance is not None and distance <= cls.MAX_FARE_LABEL_DISTANCE
        ]
        return min(candidates)[1] if candidates else None

    @classmethod
    def _near_keyword(
        cls,
        text: str,
        start: int,
        end: int,
        keywords: tuple[str, ...],
        maximum_distance: int,
    ) -> bool:
        distance = cls._keyword_distance(text, start, end, keywords)
        return distance is not None and distance <= maximum_distance

    @staticmethod
    def _keyword_distance(
        text: str,
        start: int,
        end: int,
        keywords: tuple[str, ...],
    ) -> int | None:
        center = (start + end) // 2
        distances: list[int] = []
        normalized = text.casefold()
        for keyword in keywords:
            for match in re.finditer(re.escape(keyword), normalized):
                keyword_center = (match.start() + match.end()) // 2
                distances.append(abs(center - keyword_center))
        return min(distances) if distances else None

    @classmethod
    def _blocked_transport_source(cls, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").casefold()
        return any(
            hostname == blocked or hostname.endswith(f".{blocked}")
            for blocked in cls.BLOCKED_TRANSPORT_HOSTS
        )

    @staticmethod
    def _remove_fare_outliers(
        options: list[TransportOption],
    ) -> list[TransportOption]:
        groups: dict[tuple[str, str], list[TransportOption]] = {}
        for option in options:
            groups.setdefault((option.mode, option.cabin_class), []).append(option)
        filtered: list[TransportOption] = []
        for group in groups.values():
            if len(group) < 3:
                filtered.extend(group)
                continue
            midpoint = median(option.priced_total_cost for option in group)
            filtered.extend(
                option
                for option in group
                if midpoint / 3 <= option.priced_total_cost <= midpoint * 3
            )
        return filtered or options

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

    @staticmethod
    def _demo_transport_results(request: TravelRequest) -> list[SearchResult]:
        origin_country = request.current_location.rsplit(",", maxsplit=1)[-1].strip().casefold()
        destination_country = request.destination.rsplit(",", maxsplit=1)[-1].strip().casefold()
        same_country = origin_country == destination_country
        mode = "train" if same_country else "flight"
        budget_share = 0.12 if same_country else 0.25
        per_person = round(request.budget_max * budget_share / request.travelers, 2)
        return [
            SearchResult(
                title=(
                    f"Demo {mode}: {request.current_location} to {request.destination}"
                ),
                url="https://www.google.com/travel/",
                snippet=(
                    f"Demo round-trip {mode} estimate: {request.currency} {per_person:.2f} "
                    "per person. This is sample data, not a live fare."
                ),
                source="demo",
            )
        ]
