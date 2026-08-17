import os
from pathlib import Path
from dotenv import load_dotenv


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return float(value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


load_dotenv()

# ============================================================
# PATHS / STORAGE
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = ROOT_DIR / "app"
STATIC_DIR = APP_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"

DATA_DIR = Path(os.getenv("CALL_AGENT_DATA_DIR", ROOT_DIR / "data"))
ANSWERS_WORKBOOK = Path(
    os.getenv("CALL_AGENT_ANSWERS_WORKBOOK", DATA_DIR / "call_answers.xlsx")
)
DATABASE_PATH = Path(
    os.getenv("CALL_AGENT_DATABASE_PATH", DATA_DIR / "calls.sqlite3")
)
GREETING_CACHE_DIR = Path(
    os.getenv("GREETING_CACHE_DIR", DATA_DIR / "greeting_cache")
)

# ============================================================
# SERVER / DASHBOARD AUTH
# ============================================================

HOST = os.getenv("CALL_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("CALL_AGENT_PORT", "8000"))

ADMIN_USERNAME = (
    os.getenv("ADMIN_USERNAME")
    or os.getenv("CALL_AGENT_ADMIN_USERNAME")
    or ""
)
ADMIN_PASSWORD = (
    os.getenv("ADMIN_PASSWORD")
    or os.getenv("CALL_AGENT_ADMIN_PASSWORD")
    or ""
)

# ============================================================
# CALL COORDINATOR
# ============================================================

MAX_CONCURRENT_CALLS = int(os.getenv("MAX_CONCURRENT_CALLS", "5"))
START_INTERVAL_SECONDS = float(os.getenv("START_INTERVAL_SECONDS", "2"))

RING_TIMEOUT_SECONDS = int(os.getenv("RING_TIMEOUT_SECONDS", "45"))
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "900"))

EXTRACTION_TIMEOUT_SECONDS = float(
    os.getenv("EXTRACTION_TIMEOUT_SECONDS", "30")
)
EXTRACTION_MAX_ATTEMPTS = int(
    os.getenv("EXTRACTION_MAX_ATTEMPTS", "3")
)
EXTRACTION_RETRY_DELAY_SECONDS = float(
    os.getenv("EXTRACTION_RETRY_DELAY_SECONDS", "2")
)

RECONCILIATION_MAX_ATTEMPTS = int(
    os.getenv("RECONCILIATION_MAX_ATTEMPTS", "5")
)
ABANDONED_JOB_GRACE_SECONDS = int(
    os.getenv("ABANDONED_JOB_GRACE_SECONDS", "120")
)

# ============================================================
# OPENAI
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# ============================================================
# TWILIO
# ============================================================

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Twilio Media Streams use 20 ms frames.
TWILIO_FRAME_MS = int(os.getenv("TWILIO_FRAME_MS", "20"))

# ============================================================
# ANSWERING MACHINE DETECTION
# ============================================================

AMD_ENABLED = _env_bool("AMD_ENABLED", False)
AMD_MODE = os.getenv("AMD_MODE", "Enable")

AMD_TIMEOUT_SECONDS = int(
    os.getenv("AMD_TIMEOUT_SECONDS", "30")
)
AMD_SPEECH_THRESHOLD_MS = int(
    os.getenv("AMD_SPEECH_THRESHOLD_MS", "2400")
)
AMD_SPEECH_END_THRESHOLD_MS = int(
    os.getenv("AMD_SPEECH_END_THRESHOLD_MS", "1200")
)
AMD_SILENCE_TIMEOUT_MS = int(
    os.getenv("AMD_SILENCE_TIMEOUT_MS", "5000")
)

# ============================================================
# TWILIO PLAYBACK / CLOSING
# ============================================================

# Maximum time allowed for queued goodbye audio to drain.
AGENT_PLAYBACK_DRAIN_SECONDS = float(
    os.getenv("AGENT_PLAYBACK_DRAIN_SECONDS", "15")
)

# Extra allowance after queued audio progress stops / mark round-trip.
AGENT_PLAYBACK_STALL_SECONDS = float(
    os.getenv("AGENT_PLAYBACK_STALL_SECONDS", "2.5")
)

# Small tail so Twilio does not clip the final consonant.
AGENT_PLAYBACK_TAIL_MS = int(
    os.getenv("AGENT_PLAYBACK_TAIL_MS", "250")
)

# Small real-time lead for paced outbound audio.
PLAYBACK_LEAD_MS = int(
    os.getenv("PLAYBACK_LEAD_MS", "60")
)

# ============================================================
# SOFT BARGE-IN / LOCAL VAD
# ============================================================

# Caller must sustain voiced audio for this long before we commit interruption.
BARGE_IN_CONFIRM_MS = int(
    os.getenv("BARGE_IN_CONFIRM_MS", "300")
)

# Maximum temporary pause while deciding whether sound is real speech/noise.
BARGE_IN_MAX_PAUSE_MS = int(
    os.getenv("BARGE_IN_MAX_PAUSE_MS", "900")
)

# Adaptive noise threshold controls.
BARGE_IN_NOISE_MULTIPLIER = float(
    os.getenv("BARGE_IN_NOISE_MULTIPLIER", "2.2")
)
BARGE_IN_VOICE_ENERGY_THRESHOLD = float(
    os.getenv("BARGE_IN_VOICE_ENERGY_THRESHOLD", "180")
)

# A caller frame below recent agent RMS * this margin is treated as probable echo.
BARGE_IN_ECHO_MARGIN = float(
    os.getenv("BARGE_IN_ECHO_MARGIN", "0.65")
)

# Short hangover avoids treating tiny gaps inside one spoken phrase as silence.
BARGE_IN_HANGOVER_FRAMES = int(
    os.getenv("BARGE_IN_HANGOVER_FRAMES", "4")
)



# ============================================================
# AGENT CLOSE GRACE
# ============================================================

# Small grace after the agent has explicitly spoken a terminal closing.
# This gives final TTS/audio events time to settle before shutdown.
AGENT_CLOSE_GRACE_SECONDS = float(
    os.getenv("AGENT_CLOSE_GRACE_SECONDS", "1.5")
)

# When the model requests closing before a spoken final line is observed,
# allow extra time for the fallback closing path to produce/play speech.
AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS = float(
    os.getenv("AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS", "3.0")
)


# ============================================================
# TWILIO ROUTES / METRICS / MEDIA DUMP
# ============================================================

# Secret used to sign short-lived Media Stream tokens.
# Keep the real value in .env.
STREAM_SECRET = os.getenv("STREAM_SECRET", "")

# Optional diagnostic media capture directory.
MEDIA_DUMP_DIR = Path(
    os.getenv("MEDIA_DUMP_DIR", DATA_DIR / "media_dumps")
)

# Operational metrics. Safe defaults keep the feature enabled only when
# explicitly requested in .env.
METRICS_ENABLED = _env_bool("METRICS_ENABLED", False)
METRICS_FLUSH_SECONDS = float(
    os.getenv("METRICS_FLUSH_SECONDS", "5")
)
METRICS_SILENCE_GAP_MS = int(
    os.getenv("METRICS_SILENCE_GAP_MS", "700")
)

# Twilio async Answering Machine Detection may return several machine verdicts.
# The route compares the lower-case AnsweredBy value against this set.
_AMD_TERMINAL_DEFAULT = (
    "machine_start,"
    "machine_end_beep,"
    "machine_end_silence,"
    "machine_end_other,"
    "fax"
)
AMD_TERMINAL_VERDICTS = {
    item.strip().lower()
    for item in os.getenv(
        "AMD_TERMINAL_VERDICTS",
        _AMD_TERMINAL_DEFAULT,
    ).split(",")
    if item.strip()
}

# ============================================================
# DEEPGRAM
# ============================================================

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# LISTEN
DEEPGRAM_LISTEN_MODEL = os.getenv(
    "DEEPGRAM_LISTEN_MODEL",
    "flux-general-multi",
)
DEEPGRAM_EOT_THRESHOLD = float(
    os.getenv("DEEPGRAM_EOT_THRESHOLD", "0.69")
)

# Retained for compatibility / future tuning.
DEEPGRAM_EOT_TIMEOUT_MS = int(
    os.getenv("DEEPGRAM_EOT_TIMEOUT_MS", "1500")
)
DEEPGRAM_EAGER_EOT_THRESHOLD = _optional_float(
    "DEEPGRAM_EAGER_EOT_THRESHOLD"
)
if DEEPGRAM_EAGER_EOT_THRESHOLD is None:
    DEEPGRAM_EAGER_EOT_THRESHOLD = 0.58

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

# SPEAK — Deepgram-managed ElevenLabs
DEEPGRAM_SPEAK_PROVIDER = os.getenv(
    "DEEPGRAM_SPEAK_PROVIDER",
    "cartesia",
)
DEEPGRAM_SPEAK_MODEL_ID = os.getenv(
    "DEEPGRAM_SPEAK_MODEL_ID",
    "sonic-3.5",
)
DEEPGRAM_SPEAK_VOICE_ID = os.getenv(
    "DEEPGRAM_SPEAK_VOICE_ID",
    "95d51f79-c397-46f9-b49a-23763d3eaa2d",
)
DEEPGRAM_SPEAK_LANGUAGE = os.getenv(
    "DEEPGRAM_SPEAK_LANGUAGE",
    "hi",
).strip()


# ============================================================
# CAMPAIGN SPEECH
# ============================================================

DEEPGRAM_GREETING = os.getenv(
    "DEEPGRAM_GREETING",
    "नमस्ते सर, मैं श्रुति बोल रही हूं, Amazon India se| kya meri baat {Business Name} se ho rhi hai।",
)

DEEPGRAM_FALLBACK_CLOSING = os.getenv(
    "DEEPGRAM_FALLBACK_CLOSING",
    (
        "No problem sir. Hum har new seller ko launch ke baad shuru ke "
        "2 months account management support free provide karte hain. "
        "Future mein agar aap Amazon par start karna chahein, to Wellsure "
        "se contact kar sakte hain. Thank you, goodbye."
    ),
)
