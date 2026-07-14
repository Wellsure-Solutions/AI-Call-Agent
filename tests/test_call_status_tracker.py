from app.services.call_status_tracker import CallStatusTracker


def test_call_status_tracker_keeps_latest_status_and_events():
    tracker = CallStatusTracker()

    tracker.upsert("call-1", "created", phone_number="9876543210")
    current = tracker.upsert("call-1", "dialing", call_sid="sid-1")

    assert current["status"] == "dialing"
    assert current["phone_number"] == "9876543210"
    assert current["call_sid"] == "sid-1"
    assert [event["status"] for event in current["events"]] == ["created", "dialing"]
    assert tracker.get("call-1")["status"] == "dialing"
    assert tracker.list()[0]["call_id"] == "call-1"
