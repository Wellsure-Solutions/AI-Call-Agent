import os
from deepgram.agent.v1.types import (
    AgentV1Settings,
    AgentV1SettingsAgent,
    AgentV1SettingsAudio,
    AgentV1SettingsAudioInput,
    AgentV1SettingsAudioOutput,
)
from prompts import PROMPT

# Load the API key from the environment instead of hardcoding


def get_agent_settings() -> AgentV1Settings:
    """Returns the configured Deepgram Agent settings."""
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
                    "model": "flux-general-multi",
                }
            },
            think={
                "provider": {
                    "type": "google",
                    "model": "gemini-2.5-flash-lite",
                },
                "prompt": PROMPT,
            },
            speak={
                "provider": {
                    "type": "eleven_labs",
                    "model_id": "eleven_multilingual_v2",
                    "voice_id": "IpXGk4Ks434Jj33XXcNh",
                }
            },
            greeting="Namaste ji",
        ),
    )