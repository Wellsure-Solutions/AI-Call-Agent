from __future__ import annotations

import json
import sys
import types

from answer_extractor import AnswerExtractor
from models import CallSession


def test_extracts_sales_qualification_answers_with_openai(monkeypatch):
    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(
                output_text=json.dumps(
                    {
                        "owner_confirmed": "yes",
                        "interested": "yes",
                        "already_selling_online": "no",
                        "gst_available": "yes",
                        "callback_approved": "yes",
                        "callback_time": "kal shaam 5 baje",
                    }
                )
            )

    class FakeOpenAI:
        def __init__(self, api_key):
            assert api_key == "test-key"
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

    session = CallSession(call_id="call-1")
    session.add_turn("agent", "Kya main business owner ya decision maker se baat kar raha hoon?")
    session.add_turn("user", "Haan ji main owner bol raha hoon")
    session.add_turn("agent", "Kya aap online marketplaces par apne products bechne mein interested hain?")
    session.add_turn("user", "Haan details bataiye")
    session.add_turn("agent", "Kya aap abhi kisi online platform par selling kar rahe hain?")
    session.add_turn("user", "Nahi abhi online nahi")
    session.add_turn("agent", "Kya aapke business ke paas GST registration hai?")
    session.add_turn("user", "Haan GST hai")
    session.add_turn("agent", "Kya hum callback schedule kar sakte hain?")
    session.add_turn("user", "Haan kal shaam 5 baje call karna")

    answers = AnswerExtractor(api_key="test-key", model="test-model").extract(session)

    assert captured["model"] == "test-model"
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True
    assert "Do not rely on keywords only" in captured["input"][0]["content"]
    assert answers == {
        "owner_confirmed": "yes",
        "interested": "yes",
        "already_selling_online": "no",
        "gst_available": "yes",
        "callback_approved": "yes",
        "callback_time": "kal shaam 5 baje",
    }
