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
# How many failed provider lookups before a call is quarantined instead of
# retried forever. Without a bound, one call the provider cannot resolve --
# network down, tunnel dead, credentials rotated -- occupies a capacity slot
# indefinitely, which at the default ceiling of one concurrent call stalls the
# entire queue. Quarantine neither invents a terminal status nor redials.
RECONCILIATION_MAX_ATTEMPTS = max(1, int(os.getenv("CALL_AGENT_RECONCILIATION_MAX_ATTEMPTS", "8")))
# Extra grace beyond a call's own maximum-call deadline before the sweeper
# treats it as abandoned. Only reached by calls no deadline action can see.
ABANDONED_JOB_GRACE_SECONDS = max(30.0, float(os.getenv("CALL_AGENT_ABANDONED_JOB_GRACE_SECONDS", "300")))


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
DEEPGRAM_SPEAK_VOICE_ID = os.getenv("DEEPGRAM_SPEAK_VOICE_ID", "Ms9OTvWb99V6DwRHZn6q")
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
# Spoken by us, not the model, when the model asks to hang up and then will
# not say goodbye. Observed on a real call: the customer said "ठीक है." and
# the line simply went dead. The prompt forbids that, the engine refuses the
# first such hangup and asks for a closing, and this is what happens when the
# model still says nothing -- a customer is never dropped in silence.
#
# Deliberately outcome-neutral, because at this point we do not know how the
# call went: it has to be true after a yes, a no, and a wrong number alike.
# Render it with scripts/prerender_greeting.py. Unrendered, the call closes
# silently exactly as it does today.
DEEPGRAM_FALLBACK_CLOSING = os.getenv(
    "DEEPGRAM_FALLBACK_CLOSING",
    "आपका समय देने के लिए धन्यवाद सर। आपका दिन शुभ हो।",
).strip()
# Vocabulary the recogniser is biased toward. Flux accepts these on the
# listen provider and they cost nothing per call.
#
# The campaign's own words are exactly the ones a Hindi phone line garbles,
# and the transcripts show it: "GST से लेते हैं" came back as "ऐसे से लेते
# हैं". A misheard GST answer sends the agent down the wrong branch of the
# pitch, so this is not cosmetic.
#
# Kept to genuine campaign terms. Biasing toward a word the customer is
# unlikely to say makes it more likely to be hallucinated out of noise.
DEEPGRAM_KEYTERMS = tuple(
    term.strip()
    for term in os.getenv(
        "DEEPGRAM_KEYTERMS",
        "GST,GST number,GST invoice,Amazon,Amazon Business,Amazon Pay Later,"
        "business account,personal account,cashback,bulk discount,credit limit,"
        "input credit,invoice,switch",
    ).split(",")
    if term.strip()
)
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
# When the model asks to hang up without having said anything since the
# customer last spoke, it is refused once and told to speak its closing line
# first. This bounds how long that second chance lasts: if no closing and no
# repeat request arrive, the call ends anyway. Longer than the normal grace
# because a whole turn -- LLM, TTS, and playback -- has to fit inside it.
AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS = max(
    1.0, float(os.getenv("CALL_AGENT_CLOSE_UNSPOKEN_GRACE_SECONDS", "8"))
)
# How long to wait for audio already handed to Twilio to finish playing before
# hanging up. Pacing keeps several seconds in flight by design, and the
# provider's "audio done" signal only means it stopped sending -- so without
# this the closing line is cut off mid-word. Bounded because a customer who
# already hung up will never acknowledge the remaining audio.
AGENT_PLAYBACK_DRAIN_SECONDS = max(0.0, float(os.getenv("CALL_AGENT_PLAYBACK_DRAIN_SECONDS", "30")))
# How long playback may make no progress before the drain gives up. This is
# what actually ends the wait on a live call -- the cap above is only reached
# by a line that never drains at all. It has to cover the mark round-trip,
# since the last marks come back after the final audio has played.
#
# Waiting is now driven by how much audio is genuinely queued rather than a
# flat timeout. The flat 8s this replaced was shorter than a full closing
# line: "जी बढ़िया सर, हमारी team आपको कल call करके पूरा process बता देगी। आपका
# दिन शुभ हो सर, धन्यवाद।" does not fit in it, and the observed symptom was
# exactly that -- the goodbye started, then the call dropped mid-sentence.
AGENT_PLAYBACK_STALL_SECONDS = max(0.5, float(os.getenv("CALL_AGENT_PLAYBACK_STALL_SECONDS", "3")))
# A short tail after playback reports finished, before the line drops.
# Playback is measured at our own socket and by Twilio's mark
# acknowledgements; Twilio still holds a small buffer downstream of both, so
# cutting at the exact instant the last mark returns clips the final
# consonant. Not a substitute for the drain -- it is the last few hundred
# milliseconds the drain cannot see.
AGENT_PLAYBACK_TAIL_MS = max(0, int(os.getenv("CALL_AGENT_PLAYBACK_TAIL_MS", "400")))
# How long the close may wait for the outbound pump to take everything the
# agent generated before playback is measured at all. The AI stops accepting
# audio on the provider's listener thread, which can be observed before the
# audio callbacks queued ahead of that have reached the paced sender -- so
# without this the drain can look at an empty sender and conclude there is
# nothing left to play. Short: it is a handoff between two tasks on the same
# loop, not a network wait.
AGENT_PLAYBACK_HANDOFF_SECONDS = max(0.0, float(os.getenv("CALL_AGENT_PLAYBACK_HANDOFF_SECONDS", "2")))

# Silence handling. A customer who answers and then says nothing -- put the
# phone down, walked away, a voicemail AMD missed -- otherwise holds the call
# until MAX_CALL_SECONDS. At a ceiling of one concurrent call that is the
# whole outbound queue stalled behind somebody who is not there.
#
# Neither the clock nor the count runs while the agent is speaking or while
# the customer is talking, so these are seconds of real silence.
IDLE_NUDGE_SECONDS = max(3.0, float(os.getenv("CALL_AGENT_IDLE_NUDGE_SECONDS", "10")))
IDLE_NUDGE_LIMIT = max(0, int(os.getenv("CALL_AGENT_IDLE_NUDGE_LIMIT", "3")))
# Spoken by the agent itself, in its own voice and in context, rather than
# played from a file -- an injected line the model knows it said keeps the
# conversation coherent when the customer does come back.
IDLE_NUDGE_MESSAGE = os.getenv("CALL_AGENT_IDLE_NUDGE_MESSAGE", "सर, आप line पर हैं?").strip()
# Said once before hanging up on a line nobody is answering. Not the same as
# the ordinary closing: there is no outcome to acknowledge and nobody has
# agreed to anything.
IDLE_CLOSING_MESSAGE = os.getenv(
    "CALL_AGENT_IDLE_CLOSING_MESSAGE",
    "लगता है आवाज़ नहीं आ रही। कोई बात नहीं सर, मैं बाद में call कर लूँगी। धन्यवाद।",
).strip()
# Jitter headroom: how much agent audio Twilio is allowed to hold ahead of
# real time. Pacing exactly to real time keeps its buffer at about one 20ms
# frame, so any hiccup writing to the socket -- a tunnel, a congested link, a
# busy event loop -- leaves Twilio with nothing to play and the customer hears
# a crackle or a gap.
#
# Costs barge-in responsiveness in exact proportion: a soft pause cannot stop
# audio Twilio already holds, so up to this much keeps playing after the pause
# begins. A confirmed barge-in still cuts instantly, because it sends `clear`.
# 200ms is roughly human reaction time, so the pause remains imperceptible
# while covering typical link jitter.
PLAYBACK_LEAD_MS = max(0, int(os.getenv("CALL_AGENT_PLAYBACK_LEAD_MS", "200")))

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
