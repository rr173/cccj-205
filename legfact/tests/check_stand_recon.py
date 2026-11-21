#!/usr/bin/env python3
"""Independent, hand-derived checks for the stand-occupancy reconciliation.

Two layers, both independent of the engine's own implementation:

1. **Naive interval model** — a deliberately simple brute-force evaluator
   that rebuilds measured state and attribution directly from the parsed
   input objects and compares them, on a dense grid of mixed half-open
   endpoints, against the segments the engine emitted.  It re-derives every
   rule instead of importing engine sweep code.
2. **Hand-written expectation table** for the three-unit handover fixture:
   exact segment boundaries (local wall-clock), measured state, attribution
   and reconciliation class, plus duration totals and bounds.

Run:  python3 -I tests/check_stand_recon.py [--sidecar PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact.stand_config import (OBS_IDLE, OBS_OCCUPIED, load_inputs,  # noqa: E402
                                  to_us)
from legfact.stand_store import ReconBook  # noqa: E402
from legfact.timeutil import parse_offset_datetime  # noqa: E402

FIX = os.path.abspath(os.path.join(HERE, "..", "fixtures", "stand"))
WIN_START = "2026-03-10T09:00:00+08:00"
WIN_END = "2026-03-10T11:00:00+08:00"

# Hand-written expected segments: (unit, hh:mm, hh:mm, measured, unknown_reason,
# attribution, recon_class) in LOCAL wall-clock minutes from 09:00.
EXPECTED = [
    # unit A
    ("A", "09:00", "09:25", "idle", None, "none", "match_idle"),
    ("A", "09:25", "09:30", "idle", None, "unique_visit", "mismatch"),
    ("A", "09:30", "10:05", "occupied", None, "unique_visit",
     "match_occupied"),
    ("A", "10:05", "10:10", "idle", None, "unique_visit", "mismatch"),
    ("A", "10:10", "11:00", "idle", None, "none", "match_idle"),
    # unit B
    ("B", "09:00", "09:10", "idle", None, "none", "match_idle"),
    ("B", "09:10", "09:25", "unknown", "observation_conflict", "none",
     "pending_review"),
    ("B", "09:25", "09:30", "unknown", "observation_conflict",
     "unique_visit", "pending_review"),
    ("B", "09:30", "09:40", "occupied", None, "unique_visit",
     "match_occupied"),
    ("B", "09:40", "10:05", "occupied", None, "conflict",
     "attribution_conflict"),
    ("B", "10:05", "10:10", "occupied", None, "unique_visit",
     "match_occupied"),
    ("B", "10:10", "10:20", "occupied", None, "unique_visit",
     "match_occupied"),
    ("B", "10:20", "10:25", "unknown", "waiting_first_observation",
     "unique_visit", "pending_review"),
    ("B", "10:25", "10:35", "unknown", "waiting_first_observation",
     "conflict", "attribution_conflict"),
    ("B", "10:35", "10:40", "unknown", "waiting_first_observation",
     "unique_visit", "pending_review"),
    ("B", "10:40", "11:00", "idle", None, "none", "match_idle"),
    # unit C
    ("C", "09:00", "09:30", "unknown", "waiting_first_observation", "none",
     "pending_review"),
    ("C", "09:30", "10:10", "idle", None, "none", "match_idle"),
    ("C", "10:10", "10:12", "idle", None, "unique_visit", "mismatch"),
    ("C", "10:12", "10:25", "occupied", None, "unique_visit",
     "match_occupied"),
    ("C", "10:25", "10:35", "occupied", None, "conflict",
     "attribution_conflict"),
    ("C", "10:35", "10:40", "unknown", "waiting_first_observation",
     "unique_visit", "pending_review"),
    ("C", "10:40", "10:45", "unknown", "waiting_first_observation", "none",
     "pending_review"),
    ("C", "10:45", "10:50", "occupied", None, "none",
     "unattributed_occupancy"),
    ("C", "10:50", "11:00", "unknown", "offline", "none", "pending_review"),
]

# Expected per-unit class totals in seconds.
EXPECTED_TOTALS_SEC = {
    "A": {"match_occupied": 2100, "match_idle": 4500, "mismatch": 600,
          "unattributed_occupancy": 0, "pending_review": 0,
          "attribution_conflict": 0},
    "B": {"match_occupied": 1500, "match_idle": 1800, "mismatch": 0,
          "unattributed_occupancy": 0, "pending_review": 1800,
          "attribution_conflict": 2100},
    "C": {"match_occupied": 780, "match_idle": 2400, "mismatch": 120,
          "unattributed_occupancy": 300, "pending_review": 3000,
          "attribution_conflict": 600},
}


def _hm_to_us(hm: str, win_start_us: int) -> int:
    h, m = map(int, hm.split(":"))
    minute = (h - 9) * 60 + m            # minutes after 09:00 local
    return win_start_us + minute * 60 * 1_000_000


# ============================================================ naive model
def naive_measured(inputs, unit, t, win_start_us):
    """Brute-force measured state at instant t from raw sensor records."""
    iv = next((iv for iv in inputs.online
               if iv.unit_id == unit and iv.start_us <= t
               < iv.end_us), None)
    if iv is None:
        return "unknown", "offline"
    blk = inputs.obs_blocks.get(unit)
    if blk is None:
        return "unknown", "waiting_first_observation"
    # latest observation of THIS online interval at or before t
    import bisect
    k = bisect.bisect_right(list(blk.at), t) - 1
    chosen = None
    for i in range(k, -1, -1):
        at_us = int(blk.at[i])
        if at_us < iv.start_us:
            break
        if at_us < iv.end_us:
            chosen = i
            break
    if chosen is None:
        return "unknown", "waiting_first_observation"
    # group observations at the chosen instant
    at_us = int(blk.at[chosen])
    codes = set()
    j = chosen
    while j >= 0 and int(blk.at[j]) == at_us:
        codes.add(int(blk.packed[j]) & 1)
        j -= 1
    j = chosen + 1
    while j < len(blk.at) and int(blk.at[j]) == at_us:
        codes.add(int(blk.packed[j]) & 1)
        j += 1
    if len(codes) > 1:
        return "unknown", "observation_conflict"
    return (("occupied" if 1 in codes else "idle"), None)


def naive_visits(inputs):
    """Ground intervals from the sidecar's adjudicated visits."""
    return None  # supplied by the caller from the sidecar


def check_naive(sidecar_path: str) -> list[str]:
    """Dense-grid cross-check with mixed half-open endpoints."""
    inputs = load_inputs(
        roster_path=os.path.join(FIX, "handover", "inputs", "visits.ndjson"),
        ledger_path=os.path.join(FIX, "handover", "inputs", "ledger.ndjson"),
        map_path=os.path.join(FIX, "handover", "inputs", "map.ndjson"),
        sensor_path=os.path.join(FIX, "handover", "inputs", "sensor.ndjson"))
    book = ReconBook(sidecar_path)
    h = book.header()
    w0, w1 = h["window_start_us"], h["window_end_us"]
    visits = {v["visit_id"]: v for v in book.visits()}
    segs = book.segments()

    # Build the set of every boundary plus probe points one microsecond on
    # either side, so half-open end/start semantics are exercised directly.
    probes = {w0, w1 - 1}
    for s in segs:
        probes.update((s["start_us"], s["end_us"] - 1,
                       min(w1 - 1, s["end_us"]),
                       max(w0, s["start_us"] - 1)))
    # plus a uniform 7-second grid over the 2-hour window
    step = 7 * 1_000_000
    probes.update(range(w0, w1, step))
    probes = sorted(t for t in probes if w0 <= t < w1)

    errors: list[str] = []
    for unit in ("A", "B", "C"):
        unit_segs = [s for s in segs if s["unit_id"] == unit]
        for t in probes:
            seg = next(s for s in unit_segs
                       if s["start_us"] <= t < s["end_us"])
            m_state, m_reason = naive_measured(inputs, unit, t, w0)
            if (seg["measured_state"], seg["unknown_reason"]) != \
                    (m_state, m_reason):
                errors.append(
                    f"{unit} at {t}: engine measured "
                    f"{seg['measured_state']}/{seg['unknown_reason']} but "
                    f"naive model says {m_state}/{m_reason}")

        # conservation and gap-freeness
        assert unit_segs[0]["start_us"] == w0
        assert unit_segs[-1]["end_us"] == w1
        for a, b in zip(unit_segs, unit_segs[1:]):
            if a["end_us"] != b["start_us"]:
                errors.append(f"{unit}: gap/overlap between segments")
            if a["end_us"] <= a["start_us"]:
                errors.append(f"{unit}: non-positive segment")
        if sum(s["duration_us"] for s in unit_segs) != w1 - w0:
            errors.append(f"{unit}: durations do not sum to the window")

    # attribution grid: recompute active physical claims at every probe from
    # the stored claims table (the engine's raw output, re-evaluated naively).
    import sqlite3
    conn = sqlite3.connect(sidecar_path)
    conn.row_factory = sqlite3.Row
    for t in probes:
        for unit in ("A", "B", "C"):
            rows = conn.execute(
                "SELECT * FROM recon_claims WHERE unit_id=? AND start_us<=? "
                "AND end_us>?", (unit, t, t)).fetchall()
            active_visits = {r["visit_id"] for r in rows}
            seg = next(s for s in segs
                       if s["unit_id"] == unit and s["start_us"] <= t
                       < s["end_us"])
            if not active_visits:
                exp_attr = "none"
            elif len(active_visits) == 1 and not seg["conflict_visits"]:
                exp_attr = "unique_visit"
            else:
                exp_attr = "conflict"
            # multi-stand overlay can force conflict even with one visit
            if seg["conflict_visits"]:
                exp_attr = "conflict"
            if seg["attribution"] != exp_attr:
                errors.append(
                    f"{unit} at {t}: engine attribution "
                    f"{seg['attribution']} but naive active-visit model says "
                    f"{exp_attr} (visits {sorted(active_visits)}, overlay "
                    f"{seg['conflict_visits']})")
            if exp_attr == "unique_visit" and \
                    seg["visit_id"] != next(iter(active_visits)):
                errors.append(f"{unit} at {t}: wrong unique visit_id")
    conn.close()
    book.close()
    return errors


def check_expected(sidecar_path: str) -> list[str]:
    book = ReconBook(sidecar_path)
    h = book.header()
    w0 = h["window_start_us"]
    segs = book.segments()
    errors: list[str] = []
    got = [
        (s["unit_id"], s["start_us"], s["end_us"], s["measured_state"],
         s["unknown_reason"], s["attribution"], s["recon_class"])
        for s in segs]
    want = [(u, _hm_to_us(a, w0), _hm_to_us(b, w0), ms, ur, at, cls)
            for (u, a, b, ms, ur, at, cls) in EXPECTED]
    if got != want:
        for g, w in zip(got, want):
            if g != w:
                errors.append(f"segment mismatch:\n engine {g}\n hand   {w}")
        if len(got) != len(want):
            errors.append(
                f"segment count {len(got)} != expected {len(want)}")

    summary = book.summary()
    for unit, exp in EXPECTED_TOTALS_SEC.items():
        got_t = {c: summary["per_unit_us"][unit].get(c, 0) // 1_000_000
                 for c in exp}
        if got_t != exp:
            errors.append(f"unit {unit} totals {got_t} != hand totals {exp}")
    # bounds: lower = confirmed occupied unit-seconds; upper adds unknown
    lower_s = summary["measured_occupied_lower_bound_us"] // 1_000_000
    upper_s = summary["measured_occupied_upper_bound_us"] // 1_000_000
    # bounds: lower = every segment the sensors confirm occupied, regardless
    # of reconciliation class (attribution conflicts still show measured
    # occupancy); upper additionally counts every unknown segment.
    def _dur(unit, a, b):
        ha, hb = a.split(":"), b.split(":")
        return ((int(hb[0]) - int(ha[0])) * 60 + int(hb[1]) - int(ha[1])) * 60
    exp_lower = sum(_dur(u, a, b) for (u, a, b, ms, *_rest) in EXPECTED
                    if ms == "occupied")
    exp_upper = exp_lower + sum(_dur(u, a, b)
                                for (u, a, b, ms, *_rest) in EXPECTED
                                if ms == "unknown")
    if (lower_s, upper_s) != (exp_lower, exp_upper):
        errors.append(
            f"occupied bounds ({lower_s},{upper_s}) != hand "
            f"({exp_lower},{exp_upper})")
    book.close()
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sidecar", default="/tmp/stand_recon.sqlite")
    args = ap.parse_args()
    errors = check_expected(args.sidecar) + check_naive(args.sidecar)
    if errors:
        print("FAIL")
        for e in errors:
            print(" -", e)
        return 1
    print(f"OK: {len(EXPECTED)} hand segments match, naive grid and "
          f"conservation checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
