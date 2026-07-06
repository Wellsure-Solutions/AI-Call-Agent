import types

from app.services.call_control import is_closing_call_message, is_terminal_assistant_text


def test_detects_deepgram_closing_call_function_name():
    assert is_closing_call_message(types.SimpleNamespace(name="_closing_call")) is True


def test_detects_deepgram_closing_call_text_marker():
    assert is_closing_call_message(types.SimpleNamespace(type="ConversationText"), "assistant: _closing_call()") is True


def test_ignores_normal_transcript_message():
    message = types.SimpleNamespace(type="ConversationText", content="Namaste ji")
    assert is_closing_call_message(message) is False


def test_detects_natural_terminal_assistant_text():
    assert is_terminal_assistant_text("Aapka din shubh ho!") is True
    assert is_terminal_assistant_text("The team will reach out soon.") is True
    assert is_terminal_assistant_text("Kya aap owner hain?") is False
