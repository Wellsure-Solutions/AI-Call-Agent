from deepgram.agent.v1.types import (
    AgentV1Settings,
    AgentV1SettingsAgent,
    AgentV1SettingsAudio,
    AgentV1SettingsAudioInput,
    AgentV1SettingsAudioOutput,
)

from app.core.prompts import PROMPT
from app.integrations.audio_profiles import get_audio_profile

from app.core.settings import (
    DEEPGRAM_API_KEY,
    DEEPGRAM_GREETING,
    DEEPGRAM_LISTEN_MODEL,
    DEEPGRAM_EAGER_EOT_THRESHOLD,
    DEEPGRAM_EOT_THRESHOLD,
    DEEPGRAM_EOT_TIMEOUT_MS,
    DEEPGRAM_THINK_MODEL,
    DEEPGRAM_THINK_PROVIDER,
    DEEPGRAM_THINK_TEMPERATURE,
    DEEPGRAM_SPEAK_PROVIDER,
    DEEPGRAM_SPEAK_MODEL_ID,
    DEEPGRAM_SPEAK_VOICE_ID,
)


def _listen_provider_settings() -> dict[str, object]:
    provider: dict[str, object] = {
        "type": "deepgram",
        "version": "v2",
        "model": DEEPGRAM_LISTEN_MODEL,
        "language_hints": ["hi", "en"],
        "eot_threshold": DEEPGRAM_EOT_THRESHOLD,
        "eot_timeout_ms": DEEPGRAM_EOT_TIMEOUT_MS,
    }

    if DEEPGRAM_EAGER_EOT_THRESHOLD is not None:
        provider["eager_eot_threshold"] = DEEPGRAM_EAGER_EOT_THRESHOLD

    return provider


def _lead_context_prompt(context: dict | None = None) -> str:
    if not context:
        return PROMPT

    business_name = str(context.get("business_name") or "").strip()
    category = str(context.get("category") or "").strip()
    notes = str(context.get("notes") or "").strip()

    if not (business_name or category or notes):
        return PROMPT

    personalized_prompt = PROMPT.replace(
        "{business_name}",
        business_name or "the business",
    )

    category_line = (
        f"Category: {category}\n"
        if category
        else "Category: Not provided. Keep the pitch general.\n"
    )

    return (
        personalized_prompt
        + "\n\n### CURRENT LEAD CONTEXT\n"
        + "Use this only for natural personalization. Do not read it like a form.\n"
        + f"Business Name: {business_name}\n"
        + category_line
        + f"Notes: {notes}\n"
    )


def get_agent_settings(
    context: dict | None = None,
    transport: str = "browser",
) -> AgentV1Settings:
    prompt = _lead_context_prompt(context)
    audio_profile = get_audio_profile(transport)

    return AgentV1Settings(
        audio=AgentV1SettingsAudio(
            input=AgentV1SettingsAudioInput(
                encoding=audio_profile.encoding,
                sample_rate=audio_profile.input_sample_rate,
            ),
            output=AgentV1SettingsAudioOutput(
                encoding=audio_profile.encoding,
                sample_rate=audio_profile.output_sample_rate,
                container=audio_profile.container,
            ),
        ),
        agent=AgentV1SettingsAgent(
            listen={"provider": _listen_provider_settings()},
            think={
                "provider": {
                    "type": DEEPGRAM_THINK_PROVIDER,
                    "model": DEEPGRAM_THINK_MODEL,
                    "temperature": DEEPGRAM_THINK_TEMPERATURE,
                },
                "prompt": prompt,
            },
            speak={
                "provider": {
                    "type": DEEPGRAM_SPEAK_PROVIDER,
                    "model_id": DEEPGRAM_SPEAK_MODEL_ID,
                    "voice_id": DEEPGRAM_SPEAK_VOICE_ID,
                }
            },
            greeting=DEEPGRAM_GREETING,
        ),
    )
