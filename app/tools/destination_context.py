from calendar import month_name
from datetime import date, timedelta

import httpx

from app.cache import RedisCache
from app.config import Settings
from app.models import TravelRequest, WeatherSummary


class DestinationContextTool:
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

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
        cache_key = (
            f"v2:{request.destination.casefold()}:{request.start_date.isoformat()}:"
            f"{request.end_date.isoformat()}"
        )
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return WeatherSummary.model_validate(cached)
            except (TypeError, ValueError):
                pass
        try:
            coordinates = self._geocode(request.destination)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            coordinates = None
        if coordinates is None:
            result = self._seasonal_estimate(request)
        else:
            latitude, longitude = coordinates
            try:
                if today <= request.start_date <= today + timedelta(days=15):
                    result = self._live_forecast(request, latitude, longitude, today)
                else:
                    result = self._historical_climate(
                        request,
                        latitude,
                        longitude,
                        today,
                    )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                result = self._seasonal_estimate(request)
        self.cache.set(cache_key, result.model_dump(mode="json"))
        return result

    def _live_forecast(
        self,
        request: TravelRequest,
        latitude: float,
        longitude: float,
        today: date,
    ) -> WeatherSummary:
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
            return self._seasonal_estimate(request)
        avg_high = round(sum(highs) / len(highs), 1)
        avg_low = round(sum(lows) / len(lows), 1)
        max_rain = int(max(rain)) if rain else None
        return WeatherSummary(
            source="Open-Meteo forecast",
            summary=(
                f"Forecast average {avg_low}C to {avg_high}C"
                + (f", with rain probability up to {max_rain}%." if max_rain is not None else ".")
            ),
            data_type="live_forecast",
            average_high_c=avg_high,
            average_low_c=avg_low,
            precipitation_probability_max=max_rain,
        )

    def _historical_climate(
        self,
        request: TravelRequest,
        latitude: float,
        longitude: float,
        today: date,
    ) -> WeatherSummary:
        years = list(range(today.year - 5, today.year))
        sample_dates: set[str] = set()
        for year in years:
            start = self._date_in_year(request.start_date, year)
            for offset in range(request.days):
                sample_dates.add((start + timedelta(days=offset)).isoformat())
        response = self.client.get(
            self.ARCHIVE_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": "auto",
                "start_date": min(sample_dates),
                "end_date": max(sample_dates),
                "models": "era5",
            },
        )
        response.raise_for_status()
        daily = response.json().get("daily", {})
        selected = [
            (high, low, rain)
            for value, high, low, rain in zip(
                daily.get("time") or [],
                daily.get("temperature_2m_max") or [],
                daily.get("temperature_2m_min") or [],
                daily.get("precipitation_sum") or [],
                strict=False,
            )
            if value in sample_dates and high is not None and low is not None
        ]
        if not selected:
            return self._seasonal_estimate(request)
        highs = [float(value[0]) for value in selected]
        lows = [float(value[1]) for value in selected]
        rain = [float(value[2]) for value in selected if value[2] is not None]
        avg_high = round(sum(highs) / len(highs), 1)
        avg_low = round(sum(lows) / len(lows), 1)
        avg_rain = round(sum(rain) / len(rain), 1) if rain else None
        rainy_days = (
            round(sum(value > 0.1 for value in rain) / len(rain) * 100)
            if rain
            else None
        )
        rain_summary = (
            f" Average daily precipitation was {avg_rain} mm, with measurable rain on "
            f"about {rainy_days}% of sampled days."
            if avg_rain is not None and rainy_days is not None
            else ""
        )
        return WeatherSummary(
            source="Open-Meteo ERA5 historical weather",
            summary=(
                f"Historical same-date averages across {years[0]}-{years[-1]} were "
                f"{avg_low}C to {avg_high}C.{rain_summary} This is a climate estimate, not a "
                "forecast; check a live forecast 7-10 days before departure."
            ),
            data_type="historical_climate_estimate",
            average_high_c=avg_high,
            average_low_c=avg_low,
            average_daily_precipitation_mm=avg_rain,
            historical_years=years,
        )

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
    def _date_in_year(value: date, year: int) -> date:
        try:
            return value.replace(year=year)
        except ValueError:
            return value.replace(year=year, day=28)

    @staticmethod
    def _seasonal_estimate(request: TravelRequest) -> WeatherSummary:
        month = month_name[request.start_date.month]
        return WeatherSummary(
            source="seasonal estimate (not a live forecast)",
            summary=(
                f"The trip begins in {month}. Pack layers and rain protection, and check a live "
                "forecast 7-10 days before departure."
            ),
            data_type="generic_seasonal_guidance",
        )
