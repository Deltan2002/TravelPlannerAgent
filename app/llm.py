import json
from copy import deepcopy
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from app.config import Settings

ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredLLM:
    RESPONSES_URL = "https://api.openai.com/v1/responses"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds)

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
                        "schema": self._strict_schema(output_model.model_json_schema()),
                        "strict": True,
                    }
                },
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        output_text = payload.get("output_text")
        if not output_text:
            output_text = self._extract_output_text(payload)
        if not output_text:
            raise ValueError("OpenAI returned no structured output text")
        return output_model.model_validate(json.loads(output_text))

    @classmethod
    def _strict_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(schema)

        def visit(value: Any) -> None:
            if isinstance(value, dict):
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
    def _extract_output_text(payload: dict[str, Any]) -> str | None:
        for output in payload.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        return None
