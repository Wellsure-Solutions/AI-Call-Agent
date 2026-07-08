from typing import Any

from app.core.prompts import PROMPT
from app.core.settings import (
    DEEPGRAM_API_KEY,
    DEEPGRAM_GREETING,
    DEEPGRAM_LISTEN_MODEL,
    DEEPGRAM_SPEAK_MODEL_ID,
    DEEPGRAM_SPEAK_PROVIDER,
    DEEPGRAM_SPEAK_VOICE_ID,
    DEEPGRAM_THINK_MODEL,
    DEEPGRAM_THINK_PROVIDER,
    DEEPGRAM_THINK_TEMPERATURE,
    DEEPGRAM_EAGER_EOT_THRESHOLD,
    DEEPGRAM_EOT_THRESHOLD,
)


def build_personalized_prompt(base_prompt: str, lead: dict | None = None) -> str:
    """Inject lead context into the AI prompt without changing call flow."""
    if not lead:
        return base_prompt
    name = lead.get("business_name") or lead.get("name") or "this business"
    category = lead.get("category") or "their category"
    phone = lead.get("phone") or lead.get("phone_number") or ""
    personalization = (
        f"\n\n### CURRENT BUSINESS CONTEXT\n"
        f"Business Name: {name}\n"
        f"Phone Number: {phone}\n"
        f"Business Category: {category}\n"
        "You are calling this specific business now. Use the business name naturally, "
        "personalize the introduction, and mention the category only where it sounds natural.\n"
    )
    return base_prompt + personalization


def get_agent_settings(lead: dict | None = None) -> dict[str, Any]:
    """Return Deepgram Agent settings for the current campaign prompt."""
    return {
        "audio": {
            "input": {"encoding": "linear16", "sample_rate": 48000},
            "output": {"encoding": "linear16", "sample_rate": 24000, "container": "none"},
        },
        "agent": {
            "listen": {
                "provider": {
                    "type": "deepgram",
                    "version": "v2",
                    "model": DEEPGRAM_LISTEN_MODEL,
                    "language_hints": ["hi", "en"],
                    "eot_threshold": DEEPGRAM_EOT_THRESHOLD,
                    "eager_eot_threshold": DEEPGRAM_EAGER_EOT_THRESHOLD,
                }
            },
            "think": {
                "provider": {
                    "type": DEEPGRAM_THINK_PROVIDER,
                    "model": DEEPGRAM_THINK_MODEL,
                    "temperature": DEEPGRAM_THINK_TEMPERATURE,
                },
                "prompt": build_personalized_prompt(PROMPT, lead),
            },
            "speak": {
                "provider": {
                    "type": DEEPGRAM_SPEAK_PROVIDER,
                    "model_id": DEEPGRAM_SPEAK_MODEL_ID,
                    "voice_id": DEEPGRAM_SPEAK_VOICE_ID,
                }
            },
            "greeting": DEEPGRAM_GREETING,
        },
    }
