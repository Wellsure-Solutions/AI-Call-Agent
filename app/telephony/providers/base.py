from __future__ import annotations

"""The telephony control plane: placing a call, asking about it, ending it.

Deliberately separate from `app/telephony/adapters/`, which owns the *media*
plane -- barge-in, pacing, marks, drain. The two were one class
(`TwilioAdapter`) and the join was accidental: `DurableCallCoordinator` only
ever wanted `connect()`/`fetch_status()`/`update_status()`, but constructing
those pulled in ~600 lines of audio tuning, and adding a second carrier would
have duplicated all of it.

`classify_dial_error` is the load-bearing method here. The coordinator used to
`import TwilioRestException` directly to decide whether a failed dial was a
proven rejection or an ambiguous submission. That is a carrier-specific
judgement and it now lives with the carrier. Get it wrong in the "rejected"
direction and a call that really was placed gets marked failed and its phone
freed for a redial -- i.e. a customer called twice.
"""

import re
from typing import Any, Iterable, Literal, NamedTuple, Protocol, runtime_checkable

from app.integrations.audio_profiles import AudioProfile

# What a failed dial attempt is allowed to mean.
#
#   rejected  -- the carrier is known to have refused. Terminal, no capacity
#                held, the phone is released by the normal terminal path.
#   ambiguous -- anything else. The submission may or may not have reached
#                the carrier, so the call enters NEEDS_RECONCILIATION, keeps
#                blocking its phone, and is *never* redialed.
#
# `ambiguous` is the default for everything unrecognised, in every provider.
DialErrorKind = Literal["rejected", "ambiguous"]


class DialResult(NamedTuple):
    """Outcome of a submission the carrier accepted.

    `provider_sid` of None means "accepted, but we cannot identify it" -- the
    caller must treat that as ambiguous rather than as a failure, because a
    call we cannot name may still be ringing somebody's phone.
    """

    provider_sid: str | None
    provider_status: str | None = None


@runtime_checkable
class TelephonyProvider(Protocol):
    """One carrier's control plane, in the vocabulary this codebase uses.

    Every status string crossing this boundary is already normalized into the
    set `app.storage.sqlite_store.PROVIDER_TERMINAL` draws from -- mapping a
    carrier's spelling into ours is the provider's job, not the store's and
    not the routes'.
    """

    name: str
    audio_profile: AudioProfile
    supports_amd: bool

    async def dial(
        self,
        *,
        call_id: str,
        to_number: str,
        ring_timeout: int,
        stream_url: str,
        status_callback_url: str,
    ) -> DialResult: ...

    async def fetch_status(self, provider_sid: str) -> str: ...

    async def request_terminal(self, provider_sid: str, requested: str) -> None: ...

    def classify_dial_error(self, error: Exception) -> DialErrorKind: ...

    def describe_dial_error(self, error: Exception) -> str:
        """A diagnosable, credential-free description, stored on the call row.

        `classify_dial_error` decides *what* happened to the queue;
        this decides whether an operator can find out *why*. The exception
        class name alone is not enough -- every carrier failure arrives as the
        same one or two types, so a row reading `HTTPStatusError` is
        indistinguishable from every other failure and undiagnosable without
        the carrier's own message.

        Implementations must run their output through `scrub()`.
        """
        ...

    def is_configured(self) -> tuple[bool, list[str]]:
        """(usable, missing setting names). Drives the settings UI's fail-closed
        check, so an operator cannot select a provider that cannot dial."""
        ...

    def caller_id(self) -> str:
        """The number this provider presents. Shown in the settings UI; a
        published business number, never a secret."""
        ...


# How much carrier detail is kept on a failed dial. Long enough to hold a
# real error message, short enough that `reconciliation_error` stays readable
# in the operations view.
MAX_ERROR_DETAIL = 300

# `//key:token@host` credentials, if a carrier ever echoes a URL back at us.
_USERINFO = re.compile(r"//[^/\s@]+:[^/\s@]+@")
# Our own media/callback HMACs. A carrier error body that quotes the StreamUrl
# we sent would otherwise put a live media token into the database and the
# operations view.
_QUERY_TOKEN = re.compile(r"((?:^|[?&])(?:token|expiry)=)[^&\s\"'<>]+")


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Make carrier error text safe to persist and show to an operator.

    Removes anything that could be a credential -- the caller's own API
    key/token values, URL userinfo, and the HMAC query parameters this service
    mints -- then collapses whitespace and truncates.

    Deliberately redacts by *value* rather than trying to recognise formats: a
    carrier is free to echo our request back in any shape, and the only thing
    we reliably know is what our own secrets are.
    """
    cleaned = " ".join(str(text or "").split())
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = _USERINFO.sub("//<redacted>@", cleaned)
    cleaned = _QUERY_TOKEN.sub(r"\1<redacted>", cleaned)
    if len(cleaned) > MAX_ERROR_DETAIL:
        cleaned = cleaned[:MAX_ERROR_DETAIL - 1].rstrip() + "…"
    return cleaned


def describe_error(error: Exception, secrets: Iterable[str] = ()) -> str:
    """Fallback dial-error description: the exception type and its message.

    Providers override this with something carrier-specific. The bare class
    name on its own is not diagnosable -- every failure looks identical -- so
    even the default carries the message.
    """
    message = scrub(str(error), secrets)
    name = type(error).__name__
    return f"{name}: {message}" if message else name


def terminal_request_for(status: str, pre_answer: frozenset[str]) -> str:
    """Which terminal state to ask the carrier for, given where the call is.

    A call that has not been answered yet is *canceled*; one that is connected
    is *completed*. The distinction is not cosmetic -- it is what separates
    "nobody picked up" from "we hung up on a live conversation" in every
    report downstream. Only the set of pre-answer status words differs between
    carriers, so that is the parameter.
    """
    return "canceled" if status in pre_answer else "completed"
