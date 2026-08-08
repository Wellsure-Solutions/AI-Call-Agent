import os
from pathlib import Path
from dotenv import load_dotenv


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return float(value)


load_dotenv()

ROOT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = ROOT_DIR / "app"
STATIC_DIR = APP_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"

DATA_DIR = Path(os.getenv("CALL_AGENT_DATA_DIR", ROOT_DIR / "data"))
ANSWERS_WORKBOOK = Path(
    os.getenv("CALL_AGENT_ANSWERS_WORKBOOK", DATA_DIR / "call_answers.xlsx")
)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
HOST = os.getenv("CALL_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("CALL_AGENT_PORT", "8000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "+17629999974")
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://zh5th3zd-8000.inc1.devtunnels.ms",
).rstrip("/")

# LISTEN
DEEPGRAM_LISTEN_MODEL = os.getenv(
    "DEEPGRAM_LISTEN_MODEL",
    "flux-general-multi",
)

# Balanced for Hindi/Hinglish: confident enough to avoid noise,
# but not so strict that replies feel delayed.
DEEPGRAM_EOT_THRESHOLD = float(
    os.getenv("DEEPGRAM_EOT_THRESHOLD", "0.72")
)

# Caps uncertain turns at ~1.8 seconds instead of letting some turns
# drift toward 2.3-5 seconds.
DEEPGRAM_EOT_TIMEOUT_MS = int(
    os.getenv("DEEPGRAM_EOT_TIMEOUT_MS", "1800")
)

# Keep eager/speculative EOT disabled for stability.
DEEPGRAM_EAGER_EOT_THRESHOLD = _optional_float(
    "DEEPGRAM_EAGER_EOT_THRESHOLD"
)

# THINK
DEEPGRAM_THINK_PROVIDER = os.getenv(
    "DEEPGRAM_THINK_PROVIDER",
    "open_ai",
)

DEEPGRAM_THINK_MODEL = os.getenv(
    "DEEPGRAM_THINK_MODEL",
    "gpt-4.1-mini",
)

DEEPGRAM_THINK_TEMPERATURE = float(
    os.getenv("DEEPGRAM_THINK_TEMPERATURE", "0.20")
)

# SPEAK — keep current ElevenLabs voice test unchanged
DEEPGRAM_SPEAK_PROVIDER = os.getenv(
    "DEEPGRAM_SPEAK_PROVIDER",
    "eleven_labs",
)

DEEPGRAM_SPEAK_MODEL_ID = os.getenv(
    "DEEPGRAM_SPEAK_MODEL_ID",
    "eleven_turbo_v2_5",
)

DEEPGRAM_SPEAK_VOICE_ID = os.getenv(
    "DEEPGRAM_SPEAK_VOICE_ID",
    "Ms9OTvWb99V6DwRHZn6q",
)

DEEPGRAM_GREETING = os.getenv(
    "DEEPGRAM_GREETING",
    "नमस्ते सर। मैं जानवी बोल रही हूं, Amazon Business team से। क्या अभी एक मिनट बात हो सकती है?",
)
