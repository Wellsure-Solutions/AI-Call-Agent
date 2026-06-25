from __future__ import annotations

import json
import logging
from typing import Any

from models import CallSession
from prompts import ANSWER_FIELDS
from settings import OPENAI_API_KEY, OPENAI_MODEL

logger = logging.getLogger(__name__)


class AnswerExtractionError(RuntimeError):
    """Raised when the AI extraction service cannot produce valid answers."""


class AnswerExtractor:
    """Extract campaign answers with OpenAI structured output instead of keyword rules."""

    def __init__(self, api_key: str | None = OPENAI_API_KEY, model: str = OPENAI_MODEL) -> None:
        self.api_key = api_key
        self.model = model

    def extract(self, session: CallSession) -> dict[str, str]:
        if not self.api_key:
            raise AnswerExtractionError("OPENAI_API_KEY is required for AI answer extraction.")

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency availability differs by environment
            raise AnswerExtractionError("openai package is required. Install requirements.txt.") from exc

        client = OpenAI(api_key=self.api_key)
        schema = self._json_schema()
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict post-call QA analyst for a Hindi/Hinglish sales calling agent. "
                        "Read the transcript and infer the final customer answers semantically. "
                        "Do not rely on keywords only. If the transcript does not support an answer, use unknown. "
                        "Return only values that match the provided JSON schema."
                    ),
                },
                {
                    "role": "user",
                    "content": self._build_user_prompt(session),
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "call_answer_extraction",
                    "schema": schema,
                    "strict": True,
                }
            },
        )
        answers = self._parse_response(response)
        return self._normalize_answers(answers)

    def _build_user_prompt(self, session: CallSession) -> str:
        fields = "\n".join(
            f"- {field.name}: {field.question}; allowed values: {', '.join(field.allowed_values)}"
            for field in ANSWER_FIELDS
        )
        return (
            "Extract these fields from the call transcript. Prefer the customer's latest clear answer.\n\n"
            f"Fields:\n{fields}\n\n"
            f"Call status: {session.status}\n"
            f"Transcript:\n{session.transcript_text or '[empty transcript]'}"
        )

    def _json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for field in ANSWER_FIELDS:
            required.append(field.name)
            if field.allowed_values == ("free_text",):
                properties[field.name] = {
                    "type": "string",
                    "description": f"{field.question} Use unknown when unavailable.",
                }
            else:
                properties[field.name] = {
                    "type": "string",
                    "enum": list(field.allowed_values),
                    "description": field.question,
                }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }

    def _parse_response(self, response: Any) -> dict[str, Any]:
        output_text = getattr(response, "output_text", None)
        if output_text:
            return json.loads(output_text)

        # Compatibility path for mocked SDK responses or SDK shape changes.
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    return json.loads(text)
        raise AnswerExtractionError("OpenAI response did not contain structured output text.")

    def _normalize_answers(self, answers: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for field in ANSWER_FIELDS:
            raw_value = answers.get(field.name, "unknown")
            value = str(raw_value).strip() if raw_value is not None else "unknown"
            if not value:
                value = "unknown"
            if field.allowed_values != ("free_text",) and value not in field.allowed_values:
                logger.warning("invalid_answer_value", extra={"field": field.name, "value": value})
                value = "unknown"
            normalized[field.name] = value
        return normalized

