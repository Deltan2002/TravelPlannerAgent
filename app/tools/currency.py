import logging
from datetime import date

import httpx

from app.cache import RedisCache
from app.config import Settings
from app.models import ConvertedAmount

logger = logging.getLogger(__name__)


class CurrencyConverterTool:
    RATE_URL = "https://api.frankfurter.dev/v2/rate/{base}/{quote}"
    COUNTRY_CURRENCIES = {
        "australia": "AUD",
        "canada": "CAD",
        "france": "EUR",
        "germany": "EUR",
        "india": "INR",
        "italy": "EUR",
        "japan": "JPY",
        "netherlands": "EUR",
        "singapore": "SGD",
        "spain": "EUR",
        "switzerland": "CHF",
        "thailand": "THB",
        "united arab emirates": "AED",
        "united kingdom": "GBP",
        "united states": "USD",
        "usa": "USD",
    }
    CITY_CURRENCIES = {
        "new york": "USD",
        "nyc": "USD",
    }

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        cache: RedisCache | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self.cache = cache or RedisCache(
            settings.redis_url, settings.cache_ttl_seconds, "currency"
        )

    @classmethod
    def destination_currency(cls, destination: str) -> str | None:
        normalized = destination.casefold().strip()
        country = destination.rsplit(",", maxsplit=1)[-1].casefold().strip()
        if country in cls.COUNTRY_CURRENCIES:
            return cls.COUNTRY_CURRENCIES[country]
        city = destination.split(",", maxsplit=1)[0].casefold().strip()
        return cls.CITY_CURRENCIES.get(city) or cls.CITY_CURRENCIES.get(normalized)

    def convert(
        self,
        amount: float,
        base_currency: str,
        quote_currency: str,
    ) -> ConvertedAmount | None:
        if base_currency == quote_currency:
            return ConvertedAmount(
                amount=round(amount, 2),
                currency=quote_currency,
                exchange_rate=1,
                rate_date=None,
                source="No conversion required",
            )
        if self.settings.app_mode == "demo":
            return None
        rate = self._rate(base_currency, quote_currency)
        if rate is None:
            return None
        value, rate_date = rate
        return ConvertedAmount(
            amount=round(amount * value, 2),
            currency=quote_currency,
            exchange_rate=value,
            rate_date=rate_date,
            source="Frankfurter reference rate",
        )

    def _rate(self, base_currency: str, quote_currency: str) -> tuple[float, date] | None:
        cache_key = f"{base_currency}:{quote_currency}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return float(cached["rate"]), date.fromisoformat(cached["date"])
            except (KeyError, TypeError, ValueError):
                pass
        try:
            response = self.client.get(
                self.RATE_URL.format(base=base_currency, quote=quote_currency)
            )
            response.raise_for_status()
            payload = response.json()
            rate = float(payload["rate"])
            rate_date = date.fromisoformat(payload["date"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Currency conversion failed; omitting converted total: %s", exc)
            return None
        self.cache.set(cache_key, {"rate": rate, "date": rate_date.isoformat()})
        return rate, rate_date
