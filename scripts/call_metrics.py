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
    ("metrics_provider_latency", "stt_ms", "Deepgram STT"),
    ("metrics_provider_latency", "llm_first_token_ms", "LLM first token (any)"),
    ("metrics_provider_latency", "llm_first_text_ms", "LLM first text token"),
    ("metrics_provider_latency", "tts_ttfb_ms", "TTS time to first byte"),
    ("metrics_provider_latency", "provider_total_ms", "Deepgram end-to-end (its egress)"),
]

# Targets stated in the brief, printed alongside so a regression is obvious.
TARGETS_MS: dict[str, tuple[float, float]] = {
    "eot_to_first_audio_ms": (800.0, 1200.0),
}


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


def build_report(events: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    report: dict[str, Any] = {"latency": {}, "barge_in": {}, "silence": {}, "cost": {}, "diagnostics": {}}

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
        report["silence"] = {
            "calls": len(calls),
            "gaps_per_call": summarize([float(c.get("silence_gaps", 0)) for c in calls]),
            "total_ms_per_call": summarize([float(c.get("silence_total_ms", 0)) for c in calls]),
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

    for kind in ("metrics_provider_warning", "metrics_provider_error"):
        found = Counter(p.get("code") or p.get("description", "")[:60] for p in events.get(kind, []))
        if found:
            report["diagnostics"][kind.replace("metrics_provider_", "")] = dict(found)

    report["acoustics"] = merge_histograms(events.get("metrics_acoustics", []))
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
    print("\n=== Latency (ms) ===")
    if not report["latency"]:
        print("  no instrumented turns found")
    for label, stats in report["latency"].items():
        line = f"  {label:<42} n={stats['n']:<5} p50={_fmt(stats['p50'])} p90={_fmt(stats['p90'])} p99={_fmt(stats['p99'])}"
        target = TARGETS_MS.get(stats["key"])
        if target and stats["p50"] is not None and stats["p90"] is not None:
            ok = stats["p50"] <= target[0] and stats["p90"] <= target[1]
            line += f"   target p50<={target[0]:.0f} p90<={target[1]:.0f}  [{'OK' if ok else 'MISS'}]"
        print(line)

    print("\n=== Barge-in ===")
    print(f"  decisions: {report['barge_in'].get('decisions') or 'none recorded'}")
    if "commit_rate" in report["barge_in"]:
        print(f"  commit rate: {report['barge_in']['commit_rate']}"
              f"   decisions while agent audio playing: {report['barge_in']['while_agent_playing']}")
    if "rms" in report["barge_in"]:
        stats = report["barge_in"]["rms"]
        print(f"  caller RMS at decision: p50={_fmt(stats['p50'])} p90={_fmt(stats['p90'])} max={_fmt(stats['max'])}")

    if report["silence"]:
        print("\n=== Dead air (gaps >= configured threshold) ===")
        gaps, total = report["silence"]["gaps_per_call"], report["silence"]["total_ms_per_call"]
        print(f"  gaps/call:      p50={_fmt(gaps['p50'])} p90={_fmt(gaps['p90'])} max={_fmt(gaps['max'])}")
        print(f"  total ms/call:  p50={_fmt(total['p50'])} p90={_fmt(total['p90'])} max={_fmt(total['max'])}")

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
