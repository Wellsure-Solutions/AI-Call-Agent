from app.services.transcript_sanitizer import strip_spoken_internal_commands


def test_strips_single_store_assignment_but_keeps_spoken_text():
    assert strip_spoken_internal_commands("store['interested'] = 'yes' Bahut achha.") == "Bahut achha."


def test_strips_multiple_store_assignments():
    text = "store['callback_approved'] = 'yes' store['callback_time'] = 'unknown' Bahut dhanyavaad ji."
    assert strip_spoken_internal_commands(text) == "Bahut dhanyavaad ji."


def test_keeps_normal_sales_question():
    assert strip_spoken_internal_commands("Kya aap interested hain?") == "Kya aap interested hain?"
