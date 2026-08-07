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
# A step-gated script with fixed facts and fixed objection answers gains
# nothing from sampling diversity; it only gains drift off the script.
DEEPGRAM_THINK_TEMPERATURE = float(os.getenv("DEEPGRAM_THINK_TEMPERATURE", "0.3"))
DEEPGRAM_SPEAK_PROVIDER = os.getenv("DEEPGRAM_SPEAK_PROVIDER", "eleven_labs")
# Measured against the live agent over 5 runs each, same voice, same prompt:
#   eleven_multilingual_v2  TTS time-to-first-byte  median 939ms  (max 3935ms)
#   eleven_flash_v2_5       TTS time-to-first-byte  median 240ms  (max  710ms)
# Provider end-to-end went from a 1473ms median to 732ms, and the tail
# collapsed from 5.9s to 1.5s. multilingual_v2 is the quality tier, not the
# latency tier, and after mu-law at 8 kHz its quality headroom is largely
# inaudible anyway.
DEEPGRAM_SPEAK_MODEL_ID = os.getenv("DEEPGRAM_SPEAK_MODEL_ID", "eleven_flash_v2_5")
DEEPGRAM_SPEAK_VOICE_ID = os.getenv("DEEPGRAM_SPEAK_VOICE_ID", "k2intd1ORm0YUH8etnXg")
# Pins the voice's language so it does not re-infer it per sentence. The
# campaign script is deliberately code-mixed -- Devanagari Hindi with English
# business terms ("GST invoice", "Amazon Business account", "cashback") left
# in Roman script -- and without a pin the model re-detects language at those
# words and shifts accent mid-sentence. Set to empty to let it auto-detect.
DEEPGRAM_SPEAK_LANGUAGE = os.getenv("DEEPGRAM_SPEAK_LANGUAGE", "hi").strip()
# zT03pEAEi0VHKciJODfn RAJU
# JNaMjd7t4u3EhgkVknn3 JANVI
# IpXGk4Ks434Jj33XXcNh ANJURA
# Ms9OTvWb99V6DwRHZn6q
# k2intd1ORm0YUH8etnXg ZARA
DEEPGRAM_GREETING = os.getenv(
    "DEEPGRAM_GREETING",
    "Hello Sir, मैं Shruti बोल रही हूँ Amazon Business Team से,"
)
# Where pre-rendered greeting audio lives. The greeting is the one utterance
# known before the call connects, so synthesising it after pickup puts ~2s of
# avoidable dead air at the start of every call. Rendered once by
# scripts/prerender_greeting.py; a cache miss falls back to the provider
# synthesising it as before, so this is safe to leave unpopulated.
GREETING_CACHE_DIR = Path(os.getenv("CALL_AGENT_GREETING_CACHE_DIR", DATA_DIR / "greetings"))
DEEPGRAM_EOT_THRESHOLD = float(os.getenv("DEEPGRAM_EOT_THRESHOLD", "0.7"))
# Eager end-of-turn starts the LLM before the user's turn is final. It can
# reduce latency, but it also makes short pauses sound like interruptions.
# Keep it opt-in so normal EndOfTurn detection is the safe default.
DEEPGRAM_EAGER_EOT_THRESHOLD = _optional_float("DEEPGRAM_EAGER_EOT_THRESHOLD")
# ---------------------------------------------------------------------------

TWILIO_FRAME_MS = 20
TWILIO_FRAME_BYTES = 160
# Floor for the adaptive voice threshold, not the threshold itself. The live
# value is derived from the line's measured noise floor; this only stops a
# pathologically quiet line from triggering on nothing.
BARGE_IN_VOICE_ENERGY_THRESHOLD = float(os.getenv("CALL_AGENT_BARGE_IN_ENERGY_THRESHOLD", "250"))
# How far above the measured noise floor speech has to sit. Measured on a real
# call the floor was ~42 RMS and speech peaked at 1400-2900, so 6x lands at
# ~250 -- clear of the noise, far below any actual utterance.
BARGE_IN_NOISE_MULTIPLIER = float(os.getenv("CALL_AGENT_BARGE_IN_NOISE_MULTIPLIER", "6"))
# Consecutive silent frames before a voiced run is considered over. This was
# 3 frames (60ms), which is shorter than the gap between ordinary syllables:
# replaying a real call, it shattered 25 speech segments into 94 fragments and
# no fragment ever reached the confirm threshold, so genuine interruptions
# were ignored outright. At 10 frames (200ms) the same audio yields one run
# per real utterance.
BARGE_IN_HANGOVER_FRAMES = int(os.getenv("CALL_AGENT_BARGE_IN_HANGOVER_FRAMES", "10"))
# Sustained voiced duration that confirms a real interruption. On real call
# audio, run lengths are bimodal -- backchannels ("haan", "acha") at or under
# 400ms, real turns at or over 600ms -- so this sits in the gap between them.
BARGE_IN_CONFIRM_MS = int(os.getenv("CALL_AGENT_BARGE_IN_CONFIRM_MS", "600"))
# How far an inbound frame must exceed the recently-played agent level to be
# treated as the customer rather than the agent's own echo. Well under unity
# on purpose: rejecting a real interruption is worse than admitting some
# echo, so only clearly-attenuated audio is filtered.
BARGE_IN_ECHO_MARGIN = float(os.getenv("CALL_AGENT_BARGE_IN_ECHO_MARGIN", "0.55"))
# Safety net: how long an ambiguous pause may last before playback resumes.
# Lowered because a 4s silent pause is itself the robotic failure it was
# meant to prevent.
BARGE_IN_MAX_PAUSE_MS = int(os.getenv("CALL_AGENT_BARGE_IN_MAX_PAUSE_MS", "2500"))

# After the agent asks to end the call, how long to let its closing sentence
# finish before hanging up regardless. Long enough for a goodbye, far short of
# the 900s maximum-call deadline that was previously the only way out.
AGENT_CLOSE_GRACE_SECONDS = max(1.0, float(os.getenv("CALL_AGENT_CLOSE_GRACE_SECONDS", "10")))

# ---------------------------------------------------------------------------
# Answering-machine detection
# ---------------------------------------------------------------------------
# Voicemail pickups otherwise get the full pitch, the full per-minute charge, a
# held concurrency slot, and a recorded greeting fed into answer extraction.
#
# Asynchronous by necessity: Twilio blocks the call until synchronous
# detection finishes, and its speech threshold alone defaults to 2400ms --
# which would blow the 500ms answer-to-greeting target on every human call to
# pay for the minority that are machines. With AsyncAmd the greeting starts
# immediately and the verdict arrives on a callback a moment later.
AMD_ENABLED = os.getenv("CALL_AGENT_AMD_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
# "Enable" classifies at greeting start; "DetectMessageEnd" waits for the beep,
# which is only worth its extra seconds if you intend to leave a message.
AMD_MODE = os.getenv("CALL_AGENT_AMD_MODE", "Enable")
AMD_TIMEOUT_SECONDS = max(3, min(59, int(os.getenv("CALL_AGENT_AMD_TIMEOUT_SECONDS", "20"))))
AMD_SPEECH_THRESHOLD_MS = max(1000, min(6000, int(os.getenv("CALL_AGENT_AMD_SPEECH_THRESHOLD_MS", "2400"))))
AMD_SPEECH_END_THRESHOLD_MS = max(500, min(5000, int(os.getenv("CALL_AGENT_AMD_SPEECH_END_THRESHOLD_MS", "1200"))))
AMD_SILENCE_TIMEOUT_MS = max(2000, min(10000, int(os.getenv("CALL_AGENT_AMD_SILENCE_TIMEOUT_MS", "5000"))))
# Which AnsweredBy verdicts end the call. "unknown" is deliberately excluded:
# detection failing is not evidence of a machine, and hanging up on it would
# drop real customers.
AMD_TERMINAL_VERDICTS = frozenset({"machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"})

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