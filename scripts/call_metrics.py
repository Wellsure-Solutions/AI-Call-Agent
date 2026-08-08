#!/usr/bin/env python3
"""Latency, barge-in, silence, and cost report over a batch of calls.

Reads the instrumentation events written to `call_events` and prints
p50/p90/p99 per metric. Nothing here is derived from a live provider call --
it only aggregates what was measured during real calls, so an empty report
means no instrumented calls exist yet, not that latency is zero.

    python scripts/call_metrics.py --since 2026-08-01 --limit 200
    python scripts/call_metrics.py --call-id <uuid>          # one call
    python scripts/call_metrics.py --json                    # machine-readable

The headline number is `eot_to_first_audio_ms`: the wall-clock gap between
the caller's last voiced audio frame and the first agent audio byte leaving
for Twilio. That is what the customer experiences as "how long before it
answered me", and it deliberately includes the endpointing hold, which
provider-side latency figures exclude.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.telephony.metrics import percentile  # noqa: E402

# (event name, payload key, human label). Order is report order.
LATENCY_METRICS: list[tuple[str, str, str]] = [
    ("metrics_turn", "eot_to_first_audio_ms", "EOT -> first agent audio (perceived)"),
    ("metrics_turn", "provider_signal_to_first_audio_ms", "  of which: our transport + pacing"),
    ("metrics_greeting", "bind_to_first_audio_ms", "Stream bind -> greeting audio"),
    ("metrics_answer", "answer_to_greeting_ms", "Answer -> greeting audio (what the caller hears)"),
    ("metrics_provider_latency", "stt_ms", "Deepgram STT (per partial, see note)"),
    ("metrics_provider_latency", "llm_first_token_ms", "LLM first token (any)"),
    ("metrics_provider_latency", "llm_first_text_ms", "LLM first text token"),
    ("metrics_provider_latency", "tts_ttfb_ms", "TTS time to first byte"),
    ("metrics_provider_latency", "provider_total_ms", "Deepgram end-to-end (its egress)"),
]

# Targets stated in the brief, printed alongside so a regression is obvious.
TARGETS_MS: dict[str, tuple[float, float]] = {
    "eot_to_first_audio_ms": (800.0, 1200.0),
    "answer_to_greeting_ms": (500.0, 800.0),
}

# Metrics that should exist on any real call. Reporting their absence is the
# point: a silently missing breakdown reads as "latency is fine" when it
# actually means the events never arrived. A provider-side field that stops
# being populated is invisible otherwise -- exactly how an empty LatencyReport
# went unnoticed across a whole batch.
EXPECTED_METRICS: list[tuple[str, str]] = [
    ("metrics_turn", "per-turn latency"),
    ("metrics_greeting", "greeting latency"),
    ("metrics_provider_latency", "Deepgram STT/LLM/TTS breakdown"),
    ("metrics_call", "per-call summary"),
]

# Below this many samples a percentile is not a percentile.
LOW_SAMPLE_WARNING = 10


def load_events(db_path: Path, call_ids: list[str] | None, since: str | None, limit: int) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if call_ids:
            placeholders = ",".join("?" for _ in call_ids)
            selected = [row[0] for row in connection.execute(
                f"SELECT call_id FROM calls WHERE call_id IN ({placeholders})", call_ids)]
        else:
            clause, args = "", []
            if since:
                clause, args = " WHERE created_at >= ?", [since]
            selected = [row[0] for row in connection.execute(
                f"SELECT call_id FROM calls{clause} ORDER BY created_at DESC LIMIT ?", (*args, limit))]
        if not selected:
            return {}
        placeholders = ",".join("?" for _ in selected)
        events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(
            f"SELECT call_id,event_name,metadata,timestamp FROM call_events "
            f"WHERE call_id IN ({placeholders}) ORDER BY event_id", selected
        ):
            try:
                payload = json.loads(row["metadata"]) or {}
            except json.JSONDecodeError:
                payload = {}
            payload["_call_id"] = row["call_id"]
            payload.setdefault("at", row["timestamp"])
            events[row["event_name"]].append(payload)
        return events
    finally:
        connection.close()


def summarize(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def derive_answer_to_greeting(events: dict[str, list[dict[str, Any]]]) -> None:
    """Reconstruct answer -> greeting, which no single event can record.

    The answer instant comes from Twilio's status webhook in one process and
    the first agent audio byte from the media socket in another, so the only
    thing joining them is the wall clock. This is the number the customer
    actually experiences as "how long before it said anything", and it is
    strictly larger than the stream-bind figure because Twilio's own
    answer-to-stream setup sits in between.
    """
    answered: dict[str, str] = {}
    for name in ("provider_in-progress", "provider_answered"):
        for payload in events.get(name, []):
            call_id = payload.get("_call_id")
            if call_id and call_id not in answered:
                answered[call_id] = payload.get("at", "")
    derived: list[dict[str, Any]] = []
    for payload in events.get("metrics_greeting", []):
        call_id, first_audio_at = payload.get("_call_id"), payload.get("at")
        answer_at = answered.get(call_id)
        if not (answer_at and first_audio_at):
            continue
        try:
            delta = (_parse(first_audio_at) - _parse(answer_at)).total_seconds() * 1000
        except (ValueError, TypeError):
            continue
        if 0 <= delta <= 60000:
            derived.append({"_call_id": call_id, "answer_to_greeting_ms": round(delta, 1)})
    if derived:
        events["metrics_answer"] = derived


def _parse(stamp: str):
    from datetime import datetime

    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def build_report(events: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    derive_answer_to_greeting(events)
    report: dict[str, Any] = {"latency": {}, "barge_in": {}, "silence": {}, "cost": {}, "diagnostics": {}}
    report["missing"] = [label for name, label in EXPECTED_METRICS if not events.get(name)]

    for event_name, key, label in LATENCY_METRICS:
        values = [float(p[key]) for p in events.get(event_name, []) if isinstance(p.get(key), (int, float))]
        if values:
            report["latency"][label] = {**summarize(values), "key": key}

    decisions = Counter(p.get("decision") for p in events.get("metrics_barge_in", []))
    report["barge_in"]["decisions"] = dict(decisions)
    total_decisions = sum(decisions.values())
    if total_decisions:
        # A commit that lands while agent audio is playing is either a real
        # interruption or the agent talking over itself through echo. The
        # split is the signal Phase 2C needs.
        while_playing = sum(1 for p in events.get("metrics_barge_in", []) if p.get("agent_playing"))
        report["barge_in"]["commit_rate"] = round(decisions.get("commit", 0) / total_decisions, 3)
        report["barge_in"]["while_agent_playing"] = while_playing
        rms = [float(p["rms"]) for p in events.get("metrics_barge_in", []) if isinstance(p.get("rms"), (int, float))]
        if rms:
            report["barge_in"]["rms"] = summarize(rms)

    calls = events.get("metrics_call", [])
    if calls:
        shares = [
            100.0 * float(c.get("silence_total_ms", 0)) / (float(c.get("media_seconds", 0)) * 1000)
            for c in calls if float(c.get("media_seconds", 0)) > 0
        ]
        report["silence"] = {
            "calls": len(calls),
            "gaps_per_call": summarize([float(c.get("silence_gaps", 0)) for c in calls]),
            "total_ms_per_call": summarize([float(c.get("silence_total_ms", 0)) for c in calls]),
            # The share matters more than the total: 13s of dead air in a 40s
            # call is a third of the conversation spent waiting, and that is
            # what a listener registers as robotic.
            "share_of_call_pct": summarize(shares),
        }
        report["cost"] = {
            "media_seconds": summarize([float(c.get("media_seconds", 0)) for c in calls]),
            "billable_seconds": summarize([float(c.get("billable_seconds", 0)) for c in calls]),
            "tts_characters": summarize([float(c.get("tts_characters", 0)) for c in calls]),
            "agent_turns": summarize([float(c.get("agent_turns", 0)) for c in calls]),
            "billable_seconds_total": sum(int(c.get("billable_seconds", 0)) for c in calls),
            "tts_characters_total": sum(int(c.get("tts_characters", 0)) for c in calls),
        }
        report["cost"]["end_reasons"] = dict(Counter(c.get("media_end_reason") for c in calls))
        # How often the model tried to hang up without speaking a closing.
        # A refusal is the guard working; a rising count means the prompt has
        # stopped carrying the closing rule, which is otherwise only
        # observable by listening to calls.
        # Eager end-of-turn: what it was set to, and what it cost. A resume is
        # a draft thrown away, so resumes/turns is both the wasted-LLM-call
        # rate and the "answered into a pause" rate.
        eager_settings = sorted({float(c.get("eager_eot", 0) or 0) for c in calls})
        eager_turns = sum(int(c.get("eager_turns", 0) or 0) for c in calls)
        resumes = sum(int(c.get("turn_resumes", 0) or 0) for c in calls)
        report["eager_eot"] = {
            "thresholds_in_batch": eager_settings,
            "eager_turns": eager_turns,
            "turn_resumes": resumes,
            "false_start_rate": round(resumes / eager_turns, 3) if eager_turns else None,
            "agent_turns": sum(int(c.get("agent_turns", 0) or 0) for c in calls),
        }

        refusals = sum(int(c.get("end_call_refusals", 0) or 0) for c in calls)
        report["closing"] = {
            "calls": len(calls),
            "hangups_refused_for_no_closing": refusals,
            "calls_affected": sum(1 for c in calls if int(c.get("end_call_refusals", 0) or 0)),
        }

    for kind in ("metrics_provider_warning", "metrics_provider_error"):
        found = Counter(p.get("code") or p.get("description", "")[:60] for p in events.get(kind, []))
        if found:
            report["diagnostics"][kind.replace("metrics_provider_", "")] = dict(found)

    report["acoustics"] = merge_histograms(events.get("metrics_acoustics", []))
    # Mixing a pre-rendered greeting with a live-synthesised one averages two
    # different systems into one meaningless number, so say which ran.
    sources = Counter(p.get("source", "unknown") for p in events.get("metrics_greeting", []))
    if sources:
        report["greeting_sources"] = dict(sources)
    return report


def merge_histograms(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-call RMS/echo histograms across the batch.

    `rms_agent_silent` is the honest line noise floor -- it is the
    distribution a fixed threshold has to clear. `caller_over_agent_db` is
    how loud inbound audio was relative to the agent audio playing at that
    instant; a mass of frames near 0 dB while the agent speaks is echo, not
    a customer.
    """
    merged: dict[str, Counter] = defaultdict(Counter)
    for entry in entries:
        for name in ("rms_agent_silent", "rms_agent_speaking", "caller_over_agent_db"):
            histogram = entry.get(name)
            if isinstance(histogram, dict):
                for bucket, count in histogram.items():
                    merged[name][str(bucket)] += int(count)
    return {name: dict(sorted(counter.items(), key=_bucket_sort)) for name, counter in merged.items()}


def _bucket_sort(item: tuple[str, int]) -> float:
    try:
        return float(item[0])
    except ValueError:
        return float("-inf")


def print_report(report: dict[str, Any]) -> None:
    attention: list[str] = []
    for kind, codes in (report.get("diagnostics") or {}).items():
        for code, count in codes.items():
            attention.append(f"provider {kind}: {code} x{count}")
    share = (report.get("silence") or {}).get("share_of_call_pct") or {}
    if share.get("p50") is not None and share["p50"] >= 25:
        attention.append(f"dead air is {share['p50']:.0f}% of the median call")
    greeting = report.get("greeting_sources") or {}
    if greeting.get("provider"):
        attention.append(
            f"{greeting['provider']} call(s) synthesised the greeting live -- "
            "run scripts/prerender_greeting.py"
        )
    barge_in = report.get("barge_in") or {}
    decisions = barge_in.get("decisions") or {}
    # Every pause ending in a timeout means the customer sat through the full
    # ambiguity window -- BARGE_IN_MAX_PAUSE_MS of dead air each -- and none
    # was ever honoured. That is what a caller misclassified as echo looks
    # like from here, so it is called out rather than left to be read out of
    # the decision counts.
    if decisions and barge_in.get("commit_rate") == 0.0:
        timeouts = decisions.get("timeout", 0)
        attention.append(
            f"no barge-in was ever honoured ({sum(decisions.values())} decision(s), "
            f"{timeouts} timed out) -- customers spoke over the agent and were not heard"
        )
    eager = report.get("eager_eot") or {}
    if len(eager.get("thresholds_in_batch") or []) > 1:
        # The one comparison this feature has to be judged on is before
        # against after. A batch containing both settings cannot make it.
        attention.append(
            f"batch mixes eager end-of-turn settings {eager['thresholds_in_batch']} -- "
            "latency figures here average two different systems"
        )
    if eager.get("false_start_rate") is not None and eager["false_start_rate"] >= 0.5:
        attention.append(
            f"{eager['false_start_rate']:.0%} of eager turns were false starts -- "
            "the threshold is firing inside pauses the customer had not finished"
        )
    closing = report.get("closing") or {}
    if closing.get("hangups_refused_for_no_closing"):
        attention.append(
            f"{closing['hangups_refused_for_no_closing']} hangup(s) refused across "
            f"{closing['calls_affected']} call(s) because no closing had been spoken -- "
            "the guard held, but the model is still trying to drop the line"
        )
    if attention:
        print("\n=== Needs attention ===")
        for line in attention:
            print(f"  ! {line}")

    if report.get("missing"):
        print("\n=== Missing measurements ===")
        for label in report["missing"]:
            print(f"  !! no {label} recorded -- this is absent data, not good data")

    print("\n=== Latency (ms) ===")
    if not report["latency"]:
        print("  no instrumented turns found")
    for label, stats in report["latency"].items():
        mark = " (low n)" if stats["n"] < LOW_SAMPLE_WARNING else ""
        line = f"  {label:<46} n={stats['n']:<4}{mark} p50={_fmt(stats['p50'])} p90={_fmt(stats['p90'])} p99={_fmt(stats['p99'])}"
        target = TARGETS_MS.get(stats["key"])
        if target and stats["p50"] is not None and stats["p90"] is not None:
            ok = stats["p50"] <= target[0] and stats["p90"] <= target[1]
            line += f"   target p50<={target[0]:.0f} p90<={target[1]:.0f}  [{'OK' if ok else 'MISS'}]"
        print(line)

    turns = (report["latency"].get("EOT -> first agent audio (perceived)") or {}).get("n") or 0
    partials = (report["latency"].get("Deepgram STT (per partial, see note)") or {}).get("n") or 0
    if partials > turns * 3 and turns:
        print(f"\n  note: STT is reported per partial transcript ({partials} reports across")
        print(f"        {turns} turns), so its tail reflects streaming jitter, not per-turn cost.")
        print("        Per-turn speech-to-audio cost is the Deepgram end-to-end line.")

    if report.get("greeting_sources"):
        print("\n=== Greeting path ===")
        print(f"  {report['greeting_sources']}   (cached = pre-rendered, provider = synthesised live)")

    print("\n=== Barge-in ===")
    decisions = report["barge_in"].get("decisions")
    if not decisions:
        # Not necessarily a fault: a pause only opens when Deepgram reports
        # the customer speaking over agent audio. A strictly turn-taking call
        # produces none, so this says "untested", not "working".
        print("  no decisions recorded -- nobody spoke over the agent in this batch, so")
        print("  barge-in tuning is UNTESTED here rather than confirmed working")
    else:
        print(f"  decisions: {decisions}")
    if "commit_rate" in report["barge_in"]:
        print(f"  commit rate: {report['barge_in']['commit_rate']}"
              f"   decisions while agent audio playing: {report['barge_in']['while_agent_playing']}")
    if "rms" in report["barge_in"]:
        stats = report["barge_in"]["rms"]
        print(f"  caller RMS at decision: p50={_fmt(stats['p50'])} p90={_fmt(stats['p90'])} max={_fmt(stats['max'])}")

    eager = report.get("eager_eot") or {}
    if eager.get("thresholds_in_batch"):
        print("\n=== Eager end-of-turn ===")
        active = [t for t in eager["thresholds_in_batch"] if t]
        if not active:
            print("  disabled in this batch -- these calls are the baseline to compare against")
        else:
            print(f"  threshold(s): {active}")
            if eager["eager_turns"]:
                print(f"  eager turns: {eager['eager_turns']}   resumed (draft discarded): "
                      f"{eager['turn_resumes']}   false-start rate: {eager['false_start_rate']:.0%}")
                print(f"  extra LLM calls this batch: ~{eager['turn_resumes']} on top of "
                      f"{eager['agent_turns']} agent turns")
            else:
                # Deepgram does not document whether the Agent API forwards
                # Flux's eager events or consumes them upstream. Zero here with
                # a latency improvement means the latter, not a broken setting.
                print("  no eager events reached this process -- either the Agent API handles")
                print("  the early draft upstream, or the setting did not take. Judge it on the")
                print("  EOT -> first agent audio figure above, against a disabled batch.")

    closing = report.get("closing") or {}
    if closing.get("calls"):
        print("\n=== Closing the call ===")
        refused = closing["hangups_refused_for_no_closing"]
        if refused:
            print(f"  {refused} hangup(s) refused across {closing['calls_affected']} of "
                  f"{closing['calls']} call(s) because the agent had said nothing")
            print("  since the customer's last turn. The guard held the line open and asked")
            print("  for a closing -- but the model still tried to drop the call.")
        else:
            print(f"  no hangup was attempted without a closing across {closing['calls']} call(s)")

    if report["silence"]:
        print("\n=== Dead air (gaps >= configured threshold) ===")
        gaps, total = report["silence"]["gaps_per_call"], report["silence"]["total_ms_per_call"]
        share = report["silence"].get("share_of_call_pct") or {}
        print(f"  gaps/call:      p50={_fmt(gaps['p50'])} p90={_fmt(gaps['p90'])} max={_fmt(gaps['max'])}")
        print(f"  total ms/call:  p50={_fmt(total['p50'])} p90={_fmt(total['p90'])} max={_fmt(total['max'])}")
        if share.get("p50") is not None:
            flag = "  <-- a third or more of the call is waiting" if share["p50"] >= 30 else ""
            print(f"  share of call:  p50={_fmt(share['p50'])}% p90={_fmt(share['p90'])}%{flag}")

    if report["cost"]:
        cost = report["cost"]
        print("\n=== Cost inputs ===")
        print(f"  calls: {report['silence']['calls']}   end reasons: {cost['end_reasons']}")
        print(f"  media seconds/call:    p50={_fmt(cost['media_seconds']['p50'])} p90={_fmt(cost['media_seconds']['p90'])}")
        print(f"  billable seconds/call: p50={_fmt(cost['billable_seconds']['p50'])} p90={_fmt(cost['billable_seconds']['p90'])}"
              f"   batch total={cost['billable_seconds_total']}")
        print(f"  TTS characters/call:   p50={_fmt(cost['tts_characters']['p50'])} p90={_fmt(cost['tts_characters']['p90'])}"
              f"   batch total={cost['tts_characters_total']}")
        print(f"  agent turns/call:      p50={_fmt(cost['agent_turns']['p50'])} p90={_fmt(cost['agent_turns']['p90'])}")

    if report["diagnostics"]:
        print("\n=== Provider diagnostics ===")
        for kind, codes in report["diagnostics"].items():
            print(f"  {kind}: {codes}")
        if any("SLOW_THINK" in str(code) for codes in report["diagnostics"].values() for code in codes):
            print("    SLOW_THINK_REQUEST: the LLM call itself was slow. Usually prompt")
            print("    length -- every turn re-reads the whole prompt. Check its size before")
            print("    blaming the model.")

    acoustics = report.get("acoustics") or {}
    if acoustics:
        print("\n=== Acoustics (frame counts by bucket) ===")
        for name, histogram in acoustics.items():
            print(f"  {name}: {histogram}")
    print()


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:,.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=None, help="SQLite path (default: configured CALL_AGENT_DATABASE_PATH)")
    parser.add_argument("--call-id", action="append", dest="call_ids", help="restrict to specific call ids (repeatable)")
    parser.add_argument("--since", help="only calls created at/after this ISO timestamp")
    parser.add_argument("--limit", type=int, default=200, help="most recent N calls (default 200)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args()

    db_path = args.db
    if db_path is None:
        from app.core.settings import DATABASE_PATH
        db_path = DATABASE_PATH
    if not Path(db_path).exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    events = load_events(Path(db_path), args.call_ids, args.since, args.limit)
    if not events:
        print("no calls matched; nothing to report", file=sys.stderr)
        return 1
    report = build_report(events)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
