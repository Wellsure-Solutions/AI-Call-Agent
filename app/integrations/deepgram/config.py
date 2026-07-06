from deepgram.agent.v1.types import (
    AgentV1Settings,
    AgentV1SettingsAgent,
    AgentV1SettingsAudio,
    AgentV1SettingsAudioInput,
    AgentV1SettingsAudioOutput,
)
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
)


def get_agent_settings() -> AgentV1Settings:
    """Return Deepgram Agent settings for the current campaign prompt."""
    return AgentV1Settings(
        audio=AgentV1SettingsAudio(
            input=AgentV1SettingsAudioInput(
                encoding="linear16",
                sample_rate=48000,
            ),
            output=AgentV1SettingsAudioOutput(
                encoding="linear16",
                sample_rate=24000,
                container="none",
            ),
        ),
        agent=AgentV1SettingsAgent(
            listen={
                "provider": {
                    "type": "deepgram",
                    "version": "v2",
                    "model": DEEPGRAM_LISTEN_MODEL,
                }
            },
            think={
                "provider": {
                    "type": DEEPGRAM_THINK_PROVIDER,
                    "model": DEEPGRAM_THINK_MODEL,
                    "temperature": DEEPGRAM_THINK_TEMPERATURE,
                },
                "prompt": PROMPT,
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
