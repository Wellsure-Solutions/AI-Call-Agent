from __future__ import annotations

"""The exact bytes of the Exotel dial request, pinned against a working call.

Exotel's v1 API is case-sensitive on both the path and the form field names.
The AgentStream developer guide renders everything lowercase
(`/v1/accounts/.../calls/connect`, `from`, `callerid`, `streamurl`); that
spelling was implemented first and **every dial failed**. The classic Voice v1
reference uses TitleCase, and a verified live request confirmed it -- along
with `StreamType=bidirectional`, which the classic reference does not list at
all.

The ground truth this file encodes, from a request observed returning 200 with
a Sid and a SubResourceUris/Stream entry:

    POST /v1/Accounts/{sid}/Calls/connect
    From=<number being dialled>
    CallerId=<ExoPhone>
    StreamUrl=<wss://...>
    StreamType=bidirectional

Assertions here are deliberately against **string literals, not the provider's
own constants**. A test written against the constants would follow a casing
regression instead of catching it, which is the entire failure mode this file
exists for. Do not "tidy" them into imports.
"""

import asyncio
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

from app.telephony.providers.exotel_provider import ExotelProvider

ACCOUNT = "acct-sid-not-real"

# The observed shape of a successful streaming dial: XML, `<Status>` of
# in-progress, the Sid in `<Sid>`, an empty `<To>`, and a SubResourceUris entry
# confirming the stream attached.
LIVE_XML_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<TwilioResponse>
  <Call>
    <Sid>b9b1b0a2c3d4e5f60718293a4b5c6d7e</Sid>
    <ParentCallSid/>
    <DateCreated>2026-08-22 11:04:17</DateCreated>
    <DateUpdated>2026-08-22 11:04:17</DateUpdated>
    <AccountSid>acct-sid-not-real</AccountSid>
    <To></To>
    <From>+919812345678</From>
    <PhoneNumberSid>08047000000</PhoneNumberSid>
    <Status>in-progress</Status>
    <StartTime>2026-08-22 11:04:17</StartTime>
    <EndTime/>
    <Duration/>
    <Price/>
    <Direction>outbound-api</Direction>
    <AnsweredBy/>
    <Uri>/v1/Accounts/acct-sid-not-real/Calls/b9b1b0a2c3d4e5f60718293a4b5c6d7e</Uri>
    <SubResourceUris>
      <Notifications>/v1/Accounts/acct-sid-not-real/Calls/b9b1b0a2c3d4e5f60718293a4b5c6d7e/Notifications</Notifications>
      <Stream>/v1/Accounts/acct-sid-not-real/Calls/b9b1b0a2c3d4e5f60718293a4b5c6d7e/Stream</Stream>
    </SubResourceUris>
  </Call>
</TwilioResponse>"""


def dial(handler=None) -> httpx.Request:
    """Place one dial through the real provider and return the raw request."""
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return (handler or (lambda _r: httpx.Response(200, text=LIVE_XML_RESPONSE)))(request)

    provider = ExotelProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        account_sid=ACCOUNT,
        api_key="key-not-real",
        api_token="token-not-real",
        subdomain="api.in.exotel.com",
        caller_id="+918047000000",
    )
    asyncio.run(
        provider.dial(
            call_id="call-wire-1",
            to_number="+919812345678",
            ring_timeout=45,
            stream_url="wss://calls.example.invalid/exotel/media-stream?call_id=call-wire-1&expiry=1&token=ab12",
            status_callback_url="https://calls.example.invalid/exotel/status/call-wire-1?expiry=1&token=cd34",
        )
    )
    return seen[0]


# ---------------------------------------------------------------------------
# Path casing
# ---------------------------------------------------------------------------
def test_the_path_is_titlecase_accounts_and_calls():
    """`/v1/accounts/.../calls/connect` is what the AgentStream guide shows and
    it does not work."""
    path = urlparse(str(dial().url)).path

    assert path == f"/v1/Accounts/{ACCOUNT}/Calls/connect"
    assert "/accounts/" not in path
    assert "/calls/" not in path


def test_the_dial_is_a_form_encoded_post():
    request = dial()

    assert request.method == "POST"
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")


# ---------------------------------------------------------------------------
# Field casing -- the four fields the working request proved
# ---------------------------------------------------------------------------
def test_the_four_verified_fields_are_present_and_titlecase():
    fields = dict(parse_qsl(dial().content.decode()))

    assert fields["From"] == "+919812345678"
    assert fields["CallerId"] == "+918047000000"
    assert fields["StreamUrl"].startswith("wss://")
    assert fields["StreamType"] == "bidirectional"


@pytest.mark.parametrize("wrong", ["from", "callerid", "streamurl", "streamtype",
                                   "statuscallback", "timelimit", "To", "to"])
def test_the_lowercase_spellings_are_not_sent(wrong):
    """The originally-implemented spelling. `To` is also absent on purpose:
    a streaming dial has no second leg."""
    assert wrong not in dict(parse_qsl(dial().content.decode()))


def test_the_optional_fields_are_titlecase_too():
    fields = dict(parse_qsl(dial().content.decode()))

    assert fields["StatusCallback"].startswith("https://")
    assert fields["TimeLimit"].isdigit()
    # Documented as an array; form-encoded bodies index it explicitly.
    assert fields["StatusCallbackEvents[0]"] == "terminal"


def test_the_whole_field_set_is_exactly_this():
    """Pinned as a set so an accidental extra field is caught too -- Exotel
    rejects the request outright on an unrecognised parameter."""
    assert set(dict(parse_qsl(dial().content.decode()))) == {
        "From",
        "CallerId",
        "StreamUrl",
        "StreamType",
        "StatusCallback",
        "StatusCallbackEvents[0]",
        "TimeLimit",
    }


def test_the_stream_url_survives_byte_for_byte():
    """Its query string carries the media HMAC; any re-encoding and the media
    socket rejects every stream."""
    fields = dict(parse_qsl(dial().content.decode()))
    assert fields["StreamUrl"] == (
        "wss://calls.example.invalid/exotel/media-stream?call_id=call-wire-1&expiry=1&token=ab12"
    )


# ---------------------------------------------------------------------------
# The live XML response shape
# ---------------------------------------------------------------------------
def test_the_observed_xml_response_yields_the_sid_and_status():
    seen: list[httpx.Request] = []

    def capture(request):
        seen.append(request)
        return httpx.Response(200, text=LIVE_XML_RESPONSE)

    provider = ExotelProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
        account_sid=ACCOUNT, api_key="k", api_token="t",
        subdomain="api.in.exotel.com", caller_id="+918047000000",
    )
    result = asyncio.run(provider.dial(
        call_id="c", to_number="+919812345678", ring_timeout=45,
        stream_url="wss://x/y", status_callback_url="https://x/z",
    ))

    assert result.provider_sid == "b9b1b0a2c3d4e5f60718293a4b5c6d7e"
    assert result.provider_status == "in-progress"


def test_the_empty_To_element_does_not_break_or_get_used():
    """On a streaming call `<To>` comes back empty -- there is no second leg
    for it to name. Anything correlating on it would break; the SID is the
    only thing this response has to supply."""
    assert "<To></To>" in LIVE_XML_RESPONSE

    parsed = ExotelProvider._parse_call(httpx.Response(200, text=LIVE_XML_RESPONSE))

    assert parsed["Sid"] == "b9b1b0a2c3d4e5f60718293a4b5c6d7e"
    assert parsed["Status"] == "in-progress"
    assert "To" not in parsed, "the parser must not surface an empty To"


def test_the_sid_is_taken_from_the_call_not_from_a_subresource_uri():
    """`SubResourceUris` repeats the SID inside URLs. Picking one of those up
    instead would bind a mangled identifier."""
    parsed = ExotelProvider._parse_call(httpx.Response(200, text=LIVE_XML_RESPONSE))

    assert "/" not in parsed["Sid"]
    assert parsed["Sid"].isalnum()
