from __future__ import annotations

"""Provider registry.

Providers are looked up by the name persisted on the *call row*, never by the
currently-selected setting. That is the whole point of persisting it: an
operator flipping the toggle while calls are in flight must not cause the
coordinator to ask Exotel about a Twilio CallSid, which would fail every
reconciliation attempt, exhaust the retry bound, and quarantine a healthy call
while it holds a capacity slot the entire time.
"""

from app.telephony.providers.base import (
    DialErrorKind,
    DialResult,
    TelephonyProvider,
    terminal_request_for,
)

PROVIDER_NAMES: tuple[str, ...] = ("twilio", "exotel")

# Selection modes an operator may store. `auto` is not a provider -- it is a
# routing rule that resolves to one at enqueue time.
SELECTABLE_PROVIDERS: tuple[str, ...] = PROVIDER_NAMES + ("auto",)

DEFAULT_PROVIDER = "twilio"


def get_provider(name: str) -> TelephonyProvider:
    """Construct the control plane for one carrier.

    Built per use rather than cached: the underlying SDK/HTTP clients are
    cheap, and a cached instance would pin credentials read at import time
    across a settings reload.
    """
    if name == "twilio":
        from app.telephony.providers.twilio_provider import TwilioProvider

        return TwilioProvider()
    if name == "exotel":
        from app.telephony.providers.exotel_provider import ExotelProvider

        return ExotelProvider()
    raise ValueError(f"unknown telephony provider: {name!r}")


def provider_status_report() -> list[dict[str, object]]:
    """Per-provider configuration state for the settings UI.

    Reports whether each provider *could* dial and which settings are missing
    if not. Never returns credential values -- only their names, plus the
    caller ID, which is a published business number.
    """
    report: list[dict[str, object]] = []
    for name in PROVIDER_NAMES:
        try:
            provider = get_provider(name)
        except Exception:
            report.append({"name": name, "configured": False, "missing": ["provider_unavailable"], "caller_id": ""})
            continue
        configured, missing = provider.is_configured()
        report.append(
            {
                "name": name,
                "configured": configured,
                "missing": missing,
                "caller_id": provider.caller_id(),
                "supports_amd": provider.supports_amd,
            }
        )
    return report


__all__ = [
    "DEFAULT_PROVIDER",
    "DialErrorKind",
    "DialResult",
    "PROVIDER_NAMES",
    "SELECTABLE_PROVIDERS",
    "TelephonyProvider",
    "get_provider",
    "provider_status_report",
    "terminal_request_for",
]
