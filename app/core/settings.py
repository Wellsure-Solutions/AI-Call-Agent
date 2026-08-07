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
DEEPGRAM_SPEAK_MODEL_ID = os.getenv("DEEPGRAM_SPEAK_MODEL_ID", "eleven_multilingual_v2")
DEEPGRAM_SPEAK_VOICE_ID = os.getenv("DEEPGRAM_SPEAK_VOICE_ID", "k2intd1ORm0YUH8etnXg")
# zT03pEAEi0VHKciJODfn RAJU
# JNaMjd7t4u3EhgkVknn3 JANVI
# IpXGk4Ks434Jj33XXcNh ANJURA
# Ms9OTvWb99V6DwRHZn6q
# k2intd1ORm0YUH8etnXg ZARA
DEEPGRAM_GREETING = os.getenv(
    "DEEPGRAM_GREETING",
    "Hello Sir, मैं Shruti बोल रही हूँ Amazon Business Team से,"
)
DEEPGRAM_EOT_THRESHOLD = float(os.getenv("DEEPGRAM_EOT_THRESHOLD", "0.7"))
# Eager end-of-turn starts the LLM before the user's turn is final. It can
# reduce latency, but it also makes short pauses sound like interruptions.
# Keep it opt-in so normal EndOfTurn detection is the safe default.
DEEPGRAM_EAGER_EOT_THRESHOLD = _optional_float("DEEPGRAM_EAGER_EOT_THRESHOLD")
# ---------------------------------------------------------------------------

TWILIO_FRAME_MS = 20
TWILIO_FRAME_BYTES = 160
BARGE_IN_VOICE_ENERGY_THRESHOLD = float(os.getenv("CALL_AGENT_BARGE_IN_ENERGY_THRESHOLD", "400"))
BARGE_IN_CONFIRM_MS = int(os.getenv("CALL_AGENT_BARGE_IN_CONFIRM_MS", "550"))
BARGE_IN_MAX_PAUSE_MS = int(os.getenv("CALL_AGENT_BARGE_IN_MAX_PAUSE_MS", "4000"))

# ---------------------------------------------------------------------------
# Instrumentation. Measurement only -- nothing below changes call behaviour.
# ---------------------------------------------------------------------------
# Per-turn latency, barge-in decisions, silence, and cost inputs are written
# to the existing `call_events` table as numeric payloads. On by default:
# without it there is no way to tell whether a tuning change helped.
METRICS_ENABLED = os.getenv("CALL_AGENT_METRICS_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
# A conversational gap this long or longer counts as dead air on the line.
METRICS_SILENCE_GAP_MS = int(os.getenv("CALL_AGENT_METRICS_SILENCE_GAP_MS", "1500"))
METRICS_FLUSH_SECONDS = float(os.getenv("CALL_AGENT_METRICS_FLUSH_SECONDS", "5"))
# Raw mu-law capture, for checking measured timings against what a human
# actually hears. This writes both sides of real customer conversations to
# disk, so it is off unless a directory is deliberately configured, and it
# must not be left on in production.
_MEDIA_DUMP_DIR = os.getenv("CALL_AGENT_MEDIA_DUMP_DIR", "").strip()
MEDIA_DUMP_DIR = Path(_MEDIA_DUMP_DIR) if _MEDIA_DUMP_DIR else None