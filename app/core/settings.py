import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default=None):
    """Read a setting, still accepting its historical CALL_AGENT_ name.

    This module was reorganised and dropped the CALL_AGENT_ prefix from most
    variables. Nothing failed loudly when it did: a deployment whose .env
    still used the old names simply got the defaults, silently.

    That cost a production outage. CALL_AGENT_STREAM_SECRET stopped being
    read, STREAM_SECRET fell back to "", and `valid_stream_token` returns
    False whenever the secret is empty -- so every media stream was rejected
    and every outbound call dropped about two seconds after the customer
    picked up. Browser calls kept working, because they carry no media token.

    Reading both names costs nothing and means an old .env, a new one, or a
    mixture all behave the same. A blank value counts as unset: an empty
    secret or an empty number is never what anybody meant.
    """
    for candidate in (name, f"CALL_AGENT_{name}", *_EXTRA_ALIASES.get(name, ())):
        value = os.getenv(candidate)
        if value is not None and value.strip() != "":
            return value
    return default


# Renames that were not a straight prefix drop, so the rule above misses them.
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "BARGE_IN_VOICE_ENERGY_THRESHOLD": ("CALL_AGENT_BARGE_IN_ENERGY_THRESHOLD",),
}


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return float(value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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
    _env("GREETING_CACHE_DIR", DATA_DIR / "greeting_cache")
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

MAX_CONCURRENT_CALLS = int(_env("MAX_CONCURRENT_CALLS", "5"))
START_INTERVAL_SECONDS = float(_env("START_INTERVAL_SECONDS", "2"))

RING_TIMEOUT_SECONDS = int(_env("RING_TIMEOUT_SECONDS", "45"))
MAX_CALL_SECONDS = int(_env("MAX_CALL_SECONDS", "900"))

EXTRACTION_TIMEOUT_SECONDS = float(
    _env("EXTRACTION_TIMEOUT_SECONDS", "30")
)
EXTRACTION_MAX_ATTEMPTS = int(
    _env("EXTRACTION_MAX_ATTEMPTS", "3")
)
EXTRACTION_RETRY_DELAY_SECONDS = float(
    _env("EXTRACTION_RETRY_DELAY_SECONDS", "2")
)

RECONCILIATION_MAX_ATTEMPTS = int(
    _env("RECONCILIATION_MAX_ATTEMPTS", "5")
)
ABANDONED_JOB_GRACE_SECONDS = int(
    _env("ABANDONED_JOB_GRACE_SECONDS", "120")
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
TWILIO_FRAME_MS = int(_env("TWILIO_FRAME_MS", "20"))

# ============================================================
# EXOTEL
# ============================================================
#
# Exotel is a licensed Indian carrier, which is the whole reason it is here:
# Twilio cannot originate calls to India with an Indian caller ID
# (https://www.twilio.com/en-us/guidelines/in/voice), so a +91 ExoPhone is the
# only way to show a local number to an Indian seller.

EXOTEL_ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID", "")
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY", "")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN", "")
# Which Exotel cluster the account lives on. This is per-account, not a
# preference: an account provisioned on one cluster returns 404 on the other.
# `api.exotel.com` is Singapore, `api.in.exotel.com` is Mumbai.
EXOTEL_SUBDOMAIN = _env("EXOTEL_SUBDOMAIN", "api.exotel.com")

# The ExoPhone, E.164. Note this is Exotel's `CallerId`, NOT its `From`:
# on /Calls/connect, `From` is the number being *dialled*. Twilio's to/from do
# not map across directly and getting it backwards dials your own ExoPhone.
EXOTEL_CALLER_ID = os.getenv("EXOTEL_CALLER_ID", "")

# Bytes of 16-bit PCM per outbound websocket message.
#
# Exotel requires a multiple of 320 (which is exactly one 20ms frame at 8 kHz
# 16-bit, so our mu-law frame grid lines up with theirs). Its stated minimum
# is "3.2k [100ms data]", which is self-inconsistent at 8 kHz -- 3200 bytes is
# 200ms there, and the arithmetic only works at 16 kHz. Starting at 3200
# satisfies both readings; undershooting risks jitter artefacts, and the
# barge-in path is not hostage to this either way because Exotel supports
# `clear`. Lower it only against measured eot_to_first_audio_ms and
# tts_ttfb_ms from real calls -- see docs/exotel-adapter.md.
EXOTEL_SEND_CHUNK_BYTES = int(_env("EXOTEL_SEND_CHUNK_BYTES", "3200"))

# Optional comma-separated IP allowlist for Exotel status callbacks. Empty
# disables the check, because Exotel publishes its ranges only on request.
# The HMAC query token is the primary control; this is defence in depth.
EXOTEL_CALLBACK_ALLOWED_IPS = tuple(
    item.strip()
    for item in _env("EXOTEL_CALLBACK_ALLOWED_IPS", "").split(",")
    if item.strip()
)

# ============================================================
# TELEPHONY PROVIDER SELECTION
# ============================================================

# Seed value only. It is written into the `settings` table the first time the
# database is opened and is *ignored from then on* -- once an operator saves a
# choice in the dashboard, the database is authoritative. Changing this
# variable on an existing deployment does nothing; change the provider in the
# dashboard instead. See guide.md §8.
DEFAULT_TELEPHONY_PROVIDER = _env("DEFAULT_PROVIDER", "twilio").strip().lower()

# ============================================================
# ANSWERING MACHINE DETECTION
# ============================================================

AMD_ENABLED = _env_bool("AMD_ENABLED", False)
AMD_MODE = _env("AMD_MODE", "Enable")

AMD_TIMEOUT_SECONDS = int(
    _env("AMD_TIMEOUT_SECONDS", "30")
)
AMD_SPEECH_THRESHOLD_MS = int(
    _env("AMD_SPEECH_THRESHOLD_MS", "2400")
)
AMD_SPEECH_END_THRESHOLD_MS = int(
    _env("AMD_SPEECH_END_THRESHOLD_MS", "1200")
)
AMD_SILENCE_TIMEOUT_MS = int(
    _env("AMD_SILENCE_TIMEOUT_MS", "5000")
)

# ============================================================
# TWILIO PLAYBACK / CLOSING
# ============================================================

# Maximum time allowed for queued goodbye audio to drain.
AGENT_PLAYBACK_DRAIN_SECONDS = float(
    _env("AGENT_PLAYBACK_DRAIN_SECONDS", "15")
)

# Extra allowance after queued audio progress stops / mark round-trip.
AGENT_PLAYBACK_STALL_SECONDS = float(
    _env("AGENT_PLAYBACK_STALL_SECONDS", "2.5")
)

# Small tail so Twilio does not clip the final consonant.
AGENT_PLAYBACK_TAIL_MS = int(
    _env("AGENT_PLAYBACK_TAIL_MS", "250")
)

# Small real-time lead for paced outbound audio.
PLAYBACK_LEAD_MS = int(
    _env("PLAYBACK_LEAD_MS", "60")
)

# How long the close may wait for the outbound pump to take everything the
# agent generated, before playback is measured at all. The AI stops accepting
# audio on Deepgram's listener thread, which can be observed before the audio
# callbacks queued ahead of it have reached the paced sender -- so without
# this the drain can look at an empty sender and conclude, wrongly, that
# there is nothing left to play. It is a handoff between two tasks on the
# same event loop, not a network wait.
AGENT_PLAYBACK_HANDOFF_SECONDS = float(
    _env("AGENT_PLAYBACK_HANDOFF_SECONDS", "2")
)

# ============================================================
# SOFT BARGE-IN / LOCAL VAD
# ============================================================

# Caller must sustain voiced audio for this long before we commit interruption.
#
# Measured on real call audio, run lengths are bimodal: backchannels ("haan",
# "acha") land at or under 400ms and genuine turns at or over 600ms, so this
# sits in the gap between them. At 300ms a 350ms "acha" confirms a barge-in
# and the agent stops mid-sentence, which is the behaviour this campaign
# specifically does not want.
BARGE_IN_CONFIRM_MS = int(
    _env("BARGE_IN_CONFIRM_MS", "600")
)

# Maximum temporary pause while deciding whether sound is real speech/noise.
BARGE_IN_MAX_PAUSE_MS = int(
    _env("BARGE_IN_MAX_PAUSE_MS", "900")
)

# Adaptive noise threshold controls.
BARGE_IN_NOISE_MULTIPLIER = float(
    _env("BARGE_IN_NOISE_MULTIPLIER", "2.2")
)
BARGE_IN_VOICE_ENERGY_THRESHOLD = float(
    _env("BARGE_IN_VOICE_ENERGY_THRESHOLD", "180")
)

# A caller frame below recent agent RMS * this margin is treated as probable echo.
BARGE_IN_ECHO_MARGIN = float(
    _env("BARGE_IN_ECHO_MARGIN", "0.65")
)

# Consecutive silent frames before a voiced run is considered over.
#
# This is the most load-bearing number in barge-in and the easiest to get
# wrong, because it is short gaps *inside* one phrase that it has to survive.
# Ordinary gaps between syllables run past 80ms: replayed against real call
# audio, a 4-frame hangover shatters a single utterance into eight fragments
# of ~160ms, none of which reaches the confirm threshold -- so a customer can
# talk over the agent indefinitely without being heard. At 10 frames (200ms)
# the same audio yields one run per real utterance.
BARGE_IN_HANGOVER_FRAMES = int(
    _env("BARGE_IN_HANGOVER_FRAMES", "10")
)



# ============================================================
# AGENT CLOSE GRACE
# ============================================================

# Small grace after the agent has explicitly spoken a terminal closing.
# This gives final TTS/audio events time to settle before shutdown.
AGENT_CLOSE_GRACE_SECONDS = float(
    _env("AGENT_CLOSE_GRACE_SECONDS", "1.5")
)

# When the model requests closing before a spoken final line is observed,
# allow extra time for the fallback closing path to produce/play speech.
AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS = float(
    _env("AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS", "3.0")
)


# ============================================================
# SILENT LINES
# ============================================================

# A seller who answers and then says nothing -- phone put down, walked away,
# a voicemail AMD missed -- otherwise holds the call until MAX_CALL_SECONDS.
# Neither the clock nor the count runs while the agent is speaking or while
# the seller is talking, so these are seconds of real silence.
IDLE_NUDGE_SECONDS = float(
    _env("IDLE_NUDGE_SECONDS", "10")
)
IDLE_NUDGE_LIMIT = int(
    _env("IDLE_NUDGE_LIMIT", "3")
)

# Spoken by the agent itself, in its own voice and in context, rather than
# played from a file -- an injected line the model knows it said keeps the
# conversation coherent if the seller does come back.
IDLE_NUDGE_MESSAGE = _env("IDLE_NUDGE_MESSAGE",
    "Hello sir, aap line par hain?",
).strip()

# Said once before hanging up on a line nobody is answering. Not the ordinary
# closing: there is no outcome to acknowledge and nobody agreed to anything.
IDLE_CLOSING_MESSAGE = _env("IDLE_CLOSING_MESSAGE",
    "Lagta hai awaaz nahi aa rahi. Koi baat nahi sir, main baad mein call kar leti hoon. Thank you, goodbye.",
).strip()


# ============================================================
# TWILIO ROUTES / METRICS / MEDIA DUMP
# ============================================================

# Secret used to sign short-lived Media Stream tokens.
# Keep the real value in .env.
STREAM_SECRET = _env("STREAM_SECRET", "")

# Optional diagnostic media capture directory. **None unless explicitly set.**
#
# This defaulted to `<data dir>/media_dumps`, which is never None, so
# `MediaDump.create()` -- whose only "off" condition is a None directory --
# could not be switched off. Every call on every deployment was writing both
# sides of the conversation to disk, contradicting media_dump.py's own
# docstring ("writes nothing unless CALL_AGENT_MEDIA_DUMP_DIR is set") and
# guide.md, which documents the default as disabled and warns never to leave
# it enabled in steady-state production.
_MEDIA_DUMP_DIR = _env("MEDIA_DUMP_DIR")
MEDIA_DUMP_DIR = Path(_MEDIA_DUMP_DIR) if _MEDIA_DUMP_DIR else None

# Operational metrics. Safe defaults keep the feature enabled only when
# explicitly requested in .env.
METRICS_ENABLED = _env_bool("METRICS_ENABLED", False)
METRICS_FLUSH_SECONDS = float(
    _env("METRICS_FLUSH_SECONDS", "5")
)
METRICS_SILENCE_GAP_MS = int(
    _env("METRICS_SILENCE_GAP_MS", "700")
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
    for item in _env("AMD_TERMINAL_VERDICTS",
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

# Vocabulary the recogniser is biased toward. Flux accepts these on the listen
# provider and they cost nothing per call.
#
# The campaign's own words are the ones a Hindi phone line garbles, and a
# misheard answer is not cosmetic -- which objection the seller raises is what
# chooses the whole rest of the conversation.
#
# Kept to terms a seller genuinely says. Biasing toward a word they are
# unlikely to use makes it easier to hallucinate out of line noise.
DEEPGRAM_KEYTERMS = tuple(
    term.strip()
    for term in os.getenv(
        "DEEPGRAM_KEYTERMS",
        "Wellsure,Shruti,Amazon,Seller Central,FBA,Flipkart,Meesho,"
        "referral fee,commission,returns,settlement,GST,listing,"
        "suspension,payment",
    ).split(",")
    if term.strip()
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
    "Hello Sir.",
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
