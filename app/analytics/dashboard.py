from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def build_dashboard(campaigns: list[dict[str, Any]], leads: list[dict[str, Any]], calls: list[dict[str, Any]], responses: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    response_by_call = {r.get("call_id"): r for r in responses}
    total = len(calls)
    completed = [c for c in calls if c.get("status") == "completed"]
    answered = [c for c in calls if c.get("outcome") in {"answered", "interested", "not_interested", "callback"} or c.get("answered")]
    interested = [r for r in responses if r.get("interested") is True]
    durations = [int(c.get("duration") or c.get("duration_seconds") or 0) for c in calls]
    ai_times = [int(c.get("ai_talk_time") or 0) for c in calls]
    customer_times = [int(c.get("customer_talk_time") or 0) for c in calls]

    def avg(values: list[int]) -> int:
        return round(sum(values) / len(values)) if values else 0

    sentiments = Counter((r.get("sentiment") or "neutral").lower() for r in responses)
    outcomes = Counter(c.get("outcome") or c.get("status") or "unknown" for c in calls)
    categories = Counter(l.get("category") or c.get("category") or "Unknown" for l in leads for c in [l])
    by_day: dict[str, int] = defaultdict(int)
    by_hour: dict[str, int] = defaultdict(int)
    calls_today = 0
    for call in calls:
        dt = parse_dt(call.get("started_at"))
        if dt:
            by_day[dt.date().isoformat()] += 1
            by_hour[f"{dt.hour:02d}:00"] += 1
            calls_today += int(dt.date() == today)

    objections = Counter(obj for r in responses for obj in r.get("objections", []) if obj)
    callbacks = Counter(str(r.get("callback_time")) for r in responses if r.get("callback_time"))
    campaign_names = {c.get("id"): c.get("name") for c in campaigns}
    by_campaign = Counter(campaign_names.get(c.get("campaign_id"), c.get("campaign") or "Default") for c in calls)

    active_statuses = {"dialing", "ringing", "in_progress", "active"}
    cards = {
        "total_leads": len(leads),
        "pending_leads": sum(1 for l in leads if l.get("status", "pending") == "pending"),
        "calls_today": calls_today,
        "calls_completed": len(completed),
        "calls_in_progress": sum(1 for c in calls if c.get("status") in active_statuses),
        "answered_calls": len(answered),
        "missed_calls": outcomes.get("missed", 0),
        "busy": outcomes.get("busy", 0),
        "failed": sum(1 for c in calls if c.get("status") == "failed" or c.get("outcome") == "failed"),
        "voicemail": outcomes.get("voicemail", 0),
        "interested_businesses": len(interested),
        "not_interested": sum(1 for r in responses if r.get("interested") is False),
        "callback_requested": sum(1 for r in responses if r.get("callback_requested") is True),
        "decision_maker_reached": sum(1 for r in responses if r.get("decision_maker")),
        "average_call_duration": avg(durations),
        "average_ai_talk_time": avg(ai_times),
        "average_customer_talk_time": avg(customer_times),
        "positive_sentiment": sentiments.get("positive", 0),
        "negative_sentiment": sentiments.get("negative", 0),
        "neutral_sentiment": sentiments.get("neutral", 0),
        "overall_success_rate": round((len(interested) / total) * 100, 1) if total else 0,
    }
    return {"cards": cards, "charts": {"calls_per_day": dict(sorted(by_day.items())), "calls_per_hour": dict(sorted(by_hour.items())), "calls_by_category": dict(categories), "answered_vs_missed": {"answered": len(answered), "missed": cards["missed_calls"]}, "call_outcomes": dict(outcomes), "sentiment_distribution": dict(sentiments), "top_categories": categories.most_common(10), "top_objections": objections.most_common(10), "top_callback_times": callbacks.most_common(10), "campaign_comparison": dict(by_campaign)}, "recent_activity": sorted(calls, key=lambda c: c.get("started_at", ""), reverse=True)[:10], "live_calls": [c for c in calls if c.get("status") in active_statuses]}
