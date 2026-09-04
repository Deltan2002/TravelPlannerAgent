from calendar import month_name
from datetime import date, timedelta

import httpx

from app.cache import RedisCache
from app.config import Settings
from app.models import TravelRequest, WeatherSummary


class DestinationContextTool:
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        cache: RedisCache | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self.cache = cache or RedisCache(
            settings.redis_url, settings.cache_ttl_seconds, "weather"
        )

    def get_weather(self, request: TravelRequest) -> WeatherSummary:
        if self.settings.app_mode == "demo":
            return self._seasonal_estimate(request)
        today = date.today()
        if request.start_date < today or request.start_date > today + timedelta(days=15):
            return self._seasonal_estimate(request)

        cache_key = (
            f"{request.destination.casefold()}:{request.start_date.isoformat()}:"
            f"{request.end_date.isoformat()}"
        )
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return WeatherSummary.model_validate(cached)
            except (TypeError, ValueError):
                pass

        coordinates = self._geocode(request.destination)
        if coordinates is None:
            result = self._seasonal_estimate(request)
            self.cache.set(cache_key, result.model_dump(mode="json"))
            return result
        latitude, longitude = coordinates
        end_date = min(request.end_date, today + timedelta(days=15))
        response = self.client.get(
            self.FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "start_date": request.start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        response.raise_for_status()
        daily = response.json().get("daily", {})
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_probability_max") or []
        if not highs or not lows:
            result = self._seasonal_estimate(request)
            self.cache.set(cache_key, result.model_dump(mode="json"))
            return result
        avg_high = round(sum(highs) / len(highs), 1)
        avg_low = round(sum(lows) / len(lows), 1)
        max_rain = int(max(rain)) if rain else None
        result = WeatherSummary(
            source="Open-Meteo forecast",
            summary=(
                f"Forecast average {avg_low}C to {avg_high}C"
                + (f", with rain probability up to {max_rain}%." if max_rain is not None else ".")
            ),
            average_high_c=avg_high,
            average_low_c=avg_low,
            precipitation_probability_max=max_rain,
        )
        self.cache.set(cache_key, result.model_dump(mode="json"))
        return result

    def _geocode(self, destination: str) -> tuple[float, float] | None:
        response = self.client.get(
            self.GEOCODING_URL,
            params={"name": destination, "count": 1, "language": "en", "format": "json"},
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            return None
        return float(results[0]["latitude"]), float(results[0]["longitude"])

    @staticmethod
    def _seasonal_estimate(request: TravelRequest) -> WeatherSummary:
        month = month_name[request.start_date.month]
        return WeatherSummary(
            source="seasonal estimate (not a live forecast)",
            summary=(
                f"The trip begins in {month}. Pack layers and rain protection, and check a live "
                "forecast 7-10 days before departure."
            ),
        )
