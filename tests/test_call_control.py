import types

from call_control import is_closing_call_message


def test_detects_deepgram_closing_call_function_name():
    assert is_closing_call_message(types.SimpleNamespace(name="_closing_call")) is True


def test_detects_deepgram_closing_call_text_marker():
    assert is_closing_call_message(types.SimpleNamespace(type="ConversationText"), "assistant: _closing_call()") is True


def test_ignores_normal_transcript_message():
    message = types.SimpleNamespace(type="ConversationText", content="Namaste ji")
    assert is_closing_call_message(message) is False
