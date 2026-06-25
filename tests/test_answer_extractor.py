from answer_extractor import AnswerExtractor
from models import CallSession


def test_extracts_sales_qualification_answers():
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

    answers = AnswerExtractor().extract(session)

    assert answers["owner_confirmed"] == "yes"
    assert answers["interested"] == "yes"
    assert answers["already_selling_online"] == "no"
    assert answers["gst_available"] == "yes"
    assert answers["callback_approved"] == "yes"
    assert "kal shaam" in answers["callback_time"]
    assert "5 baje" in answers["callback_time"]
