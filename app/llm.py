import json
from copy import deepcopy
from hashlib import sha256
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.cache import RedisCache
from app.config import Settings

ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredLLM:
    RESPONSES_URL = "https://api.openai.com/v1/responses"
    SUPPORTED_STRING_FORMATS = {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "ipv4",
        "ipv6",
        "uuid",
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
            settings.redis_url, settings.cache_ttl_seconds, "llm"
        )
        self.timeout = httpx.Timeout(
            connect=settings.http_timeout_seconds,
            read=settings.openai_timeout_seconds,
            write=settings.http_timeout_seconds,
            pool=settings.http_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return self.settings.llm_provider == "openai" and bool(self.settings.openai_api_key)

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_model: type[ModelT],
        schema_name: str,
    ) -> ModelT | None:
        if not self.enabled:
            return None
        output_schema = self._strict_schema(output_model.model_json_schema())
        cache_key = self._cache_key(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=schema_name,
            output_schema=output_schema,
        )
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict):
            try:
                return output_model.model_validate(cached)
            except (TypeError, ValueError):
                pass
        try:
            response = self.client.post(
                self.RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.openai_model,
                    "store": False,
                    "input": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": schema_name,
                            "schema": output_schema,
                            "strict": True,
                        }
                    },
                },
                timeout=self.timeout,
            )
        except httpx.ReadTimeout as exc:
            seconds = self.settings.openai_timeout_seconds
            raise RuntimeError(
                f"OpenAI response timed out after {seconds:g} seconds"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"OpenAI request timed out: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(self._api_error_message(response)) from exc
        payload: dict[str, Any] = response.json()
        output_text = payload.get("output_text")
        if not output_text:
            output_text = self._extract_output_text(payload)
        if not output_text:
            raise ValueError("OpenAI returned no structured output text")
        result = output_model.model_validate(json.loads(output_text))
        self.cache.set(cache_key, result.model_dump(mode="json"))
        return result

    def _cache_key(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        output_schema: dict[str, Any],
    ) -> str:
        value = json.dumps(
            {
                "model": self.settings.openai_model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema_name": schema_name,
                "schema": output_schema,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(value.encode()).hexdigest()

    @classmethod
    def _strict_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(schema)

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                value.pop("default", None)
                schema_format = value.get("format")
                if (
                    isinstance(schema_format, str)
                    and schema_format not in cls.SUPPORTED_STRING_FORMATS
                ):
                    value.pop("format")
                properties = value.get("properties")
                if isinstance(properties, dict):
                    value["additionalProperties"] = False
                    value["required"] = list(properties)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(normalized)
        return normalized

    @staticmethod
    def _api_error_message(response: httpx.Response) -> str:
        message = response.reason_phrase or "request failed"
        code: str | None = None
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                if error.get("message"):
                    message = str(error["message"])
                if error.get("code"):
                    code = str(error["code"])
        except (ValueError, TypeError):
            if response.text.strip():
                message = response.text.strip()[:500]
        suffix = f" [{code}]" if code else ""
        return f"OpenAI API returned HTTP {response.status_code}{suffix}: {message}"

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str | None:
        for output in payload.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        return None
