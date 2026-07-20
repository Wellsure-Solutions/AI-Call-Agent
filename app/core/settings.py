import os
from pathlib import Path
from dotenv import load_dotenv


def _optional_float(name: str) -> float | None:
    """Read an optional float, treating an unset or blank value as disabled."""
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
DATABASE_PATH = Path(os.getenv("CALL_AGENT_DATABASE_PATH", DATA_DIR / "call_agent.sqlite3"))
ANSWERS_WORKBOOK = Path(os.getenv("CALL_AGENT_ANSWERS_WORKBOOK", DATA_DIR / "call_answers.xlsx"))
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
HOST = os.getenv("CALL_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("CALL_AGENT_PORT", "8000"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
ADMIN_USERNAME = os.getenv("CALL_AGENT_ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("CALL_AGENT_ADMIN_PASSWORD")
STREAM_SECRET = os.getenv("CALL_AGENT_STREAM_SECRET")
MAX_CONCURRENT_CALLS = max(1, int(os.getenv("CALL_AGENT_MAX_CONCURRENT_CALLS", "1")))
START_INTERVAL_SECONDS = max(0.0, float(os.getenv("CALL_AGENT_START_INTERVAL_SECONDS", "2")))
RING_TIMEOUT_SECONDS = max(1, int(os.getenv("CALL_AGENT_RING_TIMEOUT_SECONDS", "45")))
MAX_CALL_SECONDS = max(1, int(os.getenv("CALL_AGENT_MAX_CALL_SECONDS", "900")))
EXTRACTION_TIMEOUT_SECONDS = max(1.0, float(os.getenv("CALL_AGENT_EXTRACTION_TIMEOUT_SECONDS", "30")))
EXTRACTION_MAX_ATTEMPTS = max(1, int(os.getenv("CALL_AGENT_EXTRACTION_MAX_ATTEMPTS", "3")))
EXTRACTION_RETRY_DELAY_SECONDS = max(0.0, float(os.getenv("CALL_AGENT_EXTRACTION_RETRY_DELAY_SECONDS", "5")))


DEEPGRAM_LISTEN_MODEL = os.getenv("DEEPGRAM_LISTEN_MODEL", "flux-general-multi")
DEEPGRAM_THINK_PROVIDER = os.getenv("DEEPGRAM_THINK_PROVIDER", "open_ai")
DEEPGRAM_THINK_MODEL = os.getenv("DEEPGRAM_THINK_MODEL", "gpt-5.4-mini")
DEEPGRAM_THINK_TEMPERATURE = float(os.getenv("DEEPGRAM_THINK_TEMPERATURE", "0.7"))
DEEPGRAM_SPEAK_PROVIDER = os.getenv("DEEPGRAM_SPEAK_PROVIDER", "eleven_labs")
DEEPGRAM_SPEAK_MODEL_ID = os.getenv("DEEPGRAM_SPEAK_MODEL_ID", "eleven_flash_v2_5")
DEEPGRAM_SPEAK_VOICE_ID = os.getenv("DEEPGRAM_SPEAK_VOICE_ID", "JNaMjd7t4u3EhgkVknn3")
# zT03pEAEi0VHKciJODfn
# IpXGk4Ks434Jj33XXcNh"
# Ms9OTvWb99V6DwRHZn6q
# k2intd1ORm0YUH8etnXg
DEEPGRAM_GREETING = os.getenv(
    "DEEPGRAM_GREETING",
    "नमस्ते जी, मैं Janvi बोल रही हूँ Amazon Business Team से, सर एक छोटा सा सवाल था"
)
DEEPGRAM_EOT_THRESHOLD = float(os.getenv("DEEPGRAM_EOT_THRESHOLD", "0.7"))
# Eager end-of-turn starts the LLM before the user's turn is final. It can
# reduce latency, but it also makes short pauses sound like interruptions.
# Keep it opt-in so normal EndOfTurn detection is the safe default.
DEEPGRAM_EAGER_EOT_THRESHOLD = _optional_float("DEEPGRAM_EAGER_EOT_THRESHOLD")
