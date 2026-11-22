#!/usr/bin/env python3
"""Deterministic, hand-computable fixtures for the arrival backtest.

The fixture is small enough to re-derive by hand on paper.  It encodes the
worked example from the audit brief plus every edge case the backtest must
isolate.  Everything is synthetic and deterministic; only these small files
and the generator live in the repository (large data is generated at runtime
by ``scale_backtest.py`` and never committed).

Roots (all roster rows, unknown roots retained as rows too):

  G1 (group G1)  the brief's root A: est 10:18 / sched 10:30 seen at 10:00,
                 close-out actual 10:20; plus two ETA updates that must NOT
                 pollute the 10:00 score (one received at 10:00 but effective
                 10:01, one visible only at 10:05 even though retro-effective);
  G2 (group G1)  the brief's root B: ETA explicitly cleared, sched 10:45,
                 close-out actual 10:25;
  G3 (group G2)  initial destination is another airport -> unobservable;
  G4 (group G2)  unknown root at the checkpoint, declared only in a later
                 batch -> unobservable then, and no airport root at close-out
                 for the lead whose checkpoint precedes the declaration;
  G5 (group G2)  identity association that only closes later: the message is a
                 forward reference at 10:00 and binds only after batch 3;
  G6 (group G3)  already landed and already reported by its lead-30
                 checkpoint (known landed); its earlier lead-60 checkpoint
                 precedes the landing;
  G11 (group G3) actually landed at 09:50 but the arrival is only published
                 at 10:30, so its 10:10 checkpoint cannot know it has already
                 landed -> later-known-already-landed at close-out;
  G12 (group G2) early flight: its lead-60 checkpoint precedes the first
                 batch visibility instant (no batch visible yet) while its
                 lead-30 checkpoint is exactly at a visibility instant;
  G7 (group G3)  close-out actual conflict -> close-out undetermined;
  G8 (group G3)  a definite actual later than the close-out instant but
                 already visible in a close-out batch -> undetermined with
                 the actual_after_close_out sub-reason;
  G9 (group G1)  fractional-second prediction/actual -> integer microsecond
                 errors and a group-vs-overall P90 difference;
  G10 (group G2) declared only in batch 3: its lead-60 checkpoint precedes the
                 declaration (unobservable, root unavailable) while the
                 lead-30 checkpoint is observable but has no definite actual
                 at close-out -> close-out undetermined;
  GX (group G1)  never declared anywhere -> unknown at both cutoffs (the
                 close-out "no airport operating root" sub-reason).

Run:  python3 -I tests/generate_backtest_fixtures.py [out_dir]
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from legfact.manifest import build_manifest, write_manifest  # noqa: E402
from generate_fixtures import (aidx_xml, identity_file,  # noqa: E402
                               rel_initial, write_batch)

AIRPORT = "CAN"
OTHER_AIRPORT = "SZX"
AUTH = "CAN-OCC"
DAY = "2026-05-12"
T0 = f"{DAY}T06:00:00+08:00"          # identity valid_from / identity_at
OFFSET = "+08:00"


def _ts(hhmm: str, sec: str = "00", frac: str | None = None) -> str:
    h, m = hhmm.split(":")
    base = f"{DAY}T{h}:{m}:{sec}"
    return base + (f".{frac}" if frac else "") + OFFSET


def _key(fno: str, arr: str = AIRPORT) -> dict:
    return {"carrier": "CA", "flight_no": fno, "suffix": "",
            "service_date": DAY, "departure": "PEK", "arrival": arr}


def _msg(event_id, fno, *, eff, times, rev=1, ident=T0, arr=AIRPORT,
         nil_times=()) -> tuple[str, str]:
    xml = aidx_xml(event_id=event_id, sender=AUTH, revision=rev,
                   effective_at=eff, identity_at=ident, carrier="CA",
                   flight_no=fno, suffix="", service_date=DAY,
                   dep="PEK", arr=arr, times=times, nil_times=nil_times)
    return xml, "aidx_xml"


# ----------------------------------------------------------------- warehouse
def build_warehouse(base: str) -> None:
    # ---- Batch 1 (visible 08:00): roots G1,G2,G3,G5(forward),G6,G7,G8,G9
    rels_b1 = [
        rel_initial("IL-G1", "G1", _key("5001"), T0, AUTH, "J1"),
        rel_initial("IL-G2", "G2", _key("5002"), T0, AUTH, "J2"),
        rel_initial("IL-G3", "G3", _key("5003", OTHER_AIRPORT), T0,
                    AUTH, "J3"),
        # G5's operating key 5005 is deliberately NOT declared yet; its
        # message below is a forward reference until batch 3.
        rel_initial("IL-G6", "G6", _key("5006"), T0, AUTH, "J6"),
        rel_initial("IL-G7", "G7", _key("5007"), T0, AUTH, "J7"),
        rel_initial("IL-G8", "G8", _key("5008"), T0, AUTH, "J8"),
        rel_initial("IL-G9", "G9", _key("5009"), T0, AUTH, "J9"),
        rel_initial("IL-G11", "G11", _key("5011"), T0, AUTH, "J11"),
        rel_initial("IL-G12", "G12", _key("5012"), T0, AUTH, "J12"),
    ]
    files_b1 = {
        "identity_b1.json": (identity_file(rels_b1), "identity_json"),
        # G1: scheduled 10:30, estimate 10:18 (effective 09:00)
        "msg_g1_base.xml": _msg(
            "E-G1-BASE", "5001", eff=_ts("09:00"),
            times={"sched_arr": _ts("10:30"), "est_arr": _ts("10:18")}),
        # G2: scheduled 10:45, estimate 10:40 (effective 09:05)
        "msg_g2_base.xml": _msg(
            "E-G2-BASE", "5002", eff=_ts("09:05"),
            times={"sched_arr": _ts("10:45"), "est_arr": _ts("10:40")}),
        # G3: scheduled at the other airport (proves airport mismatch uses the
        # INITIAL destination, not whatever fields later messages carry)
        "msg_g3_base.xml": _msg(
            "E-G3-BASE", "5003", eff=_ts("09:00"), arr=OTHER_AIRPORT,
            times={"sched_arr": _ts("10:30")}),
        # G5: message to a key no root owns yet -> forward at 10:00
        "msg_g5_forward.xml": _msg(
            "E-G5-FWD", "5005", eff=_ts("09:10"),
            times={"sched_arr": _ts("10:50"), "est_arr": _ts("10:52")}),
        # G6: actual 09:40 delivered early (batch 1). Roster time 10:20 makes
        # the lead-30 checkpoint 09:50 (already landed and already reported ->
        # known landed); the lead-60 checkpoint 09:20 precedes the landing.
        "msg_g6_act.xml": _msg(
            "E-G6-ACT", "5006", eff=_ts("09:40"),
            times={"sched_arr": _ts("10:20"), "act_arr": _ts("09:40")}),
        # G11: declared now with schedule only; its 09:50 actual is delivered
        # late in batch 5, so an officer at its 10:10 checkpoint cannot know it
        # has already landed (later-known-already-landed at close-out).
        "msg_g11_base.xml": _msg(
            "E-G11-BASE", "5011", eff=_ts("09:00"),
            times={"sched_arr": _ts("10:40")}),
        # G12: early flight whose lead-60 checkpoint precedes the first batch
        # visibility instant while still lying inside the claimed coverage.
        "msg_g12_base.xml": _msg(
            "E-G12-BASE", "5012", eff=_ts("06:30"),
            times={"sched_arr": _ts("08:30"), "est_arr": _ts("08:34"),
                   "act_arr": _ts("08:35")}),
        # G7: first actual contender 10:25 (a second, conflicting contender
        # arrives in batch 4 with the SAME effective priority)
        "msg_g7_act_a.xml": _msg(
            "E-G7-ACT-A", "5007", eff=_ts("10:25"),
            times={"sched_arr": _ts("10:35"), "act_arr": _ts("10:25")}),
        # G8: scheduled only in batch 1; its actual is a future-dated report
        # delivered in batch 4 (visible 10:05) with effective 11:20, so at the
        # 11:00 close-out a definite actual EXISTS but lies after the instant.
        "msg_g8_base.xml": _msg(
            "E-G8-BASE", "5008", eff=_ts("09:00"),
            times={"sched_arr": _ts("11:30")}),
        # G9: fractional-second estimate (10:32.500000) and schedule 10:35
        "msg_g9_base.xml": _msg(
            "E-G9-BASE", "5009", eff=_ts("09:00"),
            times={"sched_arr": _ts("10:35"),
                   "est_arr": _ts("10:32", frac="500000")}),
    }
    write_batch(os.path.join(base, "wh"), 1, AUTH, files_b1)

    # ---- Batch 2 (visible exactly 10:00): the brief's non-polluting ETA.
    # It is already visible at the 10:00 checkpoint but only EFFECTIVE 10:01,
    # so it must not enter the 10:00 facts; it DOES enter the 10:30 facts.
    files_b2 = {
        "msg_g1_future_eff.xml": _msg(
            "E-G1-FUT-EFF", "5001", eff=_ts("10:01"), rev=2,
            times={"est_arr": _ts("10:16")}),
    }
    write_batch(os.path.join(base, "wh"), 2, AUTH, files_b2)

    # ---- Batch 3 (visible 10:02): closes G5's identity and declares G4/G10.
    rels_b3 = [
        rel_initial("IL-G4", "G4", _key("5004"), T0, AUTH, "J4"),
        rel_initial("IL-G5", "G5", _key("5005"), T0, AUTH, "J5"),
        rel_initial("IL-G10", "G10", _key("5010"), T0, AUTH, "J10"),
    ]
    files_b3 = {
        "identity_b3.json": (identity_file(rels_b3), "identity_json"),
        # G4 only starts existing now; its estimate cannot be seen at 10:00
        "msg_g4_base.xml": _msg(
            "E-G4-BASE", "5004", eff=_ts("10:02"),
            times={"sched_arr": _ts("11:00"), "est_arr": _ts("11:05")}),
        # G5 now binds; its facts remain effective 09:10 (in the past) but the
        # binding did not exist at the 10:00 knowledge cut.
        "msg_g5_sched.xml": _msg(
            "E-G5-SCHED", "5005", eff=_ts("10:02"),
            times={"sched_arr": _ts("10:50")}),
        # G10 is only declared here and never gets a definite actual: its
        # later checkpoint is observable but undetermined at close-out, while
        # its earlier checkpoint (before 10:02) is unobservable.
        "msg_g10_base.xml": _msg(
            "E-G10-BASE", "5010", eff=_ts("10:02"),
            times={"sched_arr": _ts("11:10"), "est_arr": _ts("11:12")}),
    }
    write_batch(os.path.join(base, "wh"), 3, AUTH, files_b3)

    # ---- Batch 4 (visible 10:05): retroactive ETA + G2 clear + G7 conflict.
    files_b4 = {
        # received 10:05 but claiming effect from 09:30 (retroactive); at the
        # 10:00 checkpoint this batch was not visible, so 10:18 stays the ETA.
        "msg_g1_retro.xml": _msg(
            "E-G1-RETRO", "5001", eff=_ts("09:30"), rev=3,
            times={"est_arr": _ts("10:12")}),
        # G2's estimate is explicitly cleared (effective 10:04)
        "msg_g2_clear.xml": _msg(
            "E-G2-CLEAR", "5002", eff=_ts("10:04"), rev=2,
            times={}, nil_times=("est_arr",)),
        # G7: same effective_at and same revision as E-G7-ACT-A but a
        # different actual value -> field conflict at close-out
        "msg_g7_act_b.xml": _msg(
            "E-G7-ACT-B", "5007", eff=_ts("10:25"), rev=1,
            times={"act_arr": _ts("10:28")}),
        # G9 close-out answer with fractional seconds (10:33.250000)
        "msg_g9_act.xml": _msg(
            "E-G9-ACT", "5009", eff=_ts("10:33"),
            times={"act_arr": _ts("10:33", frac="250000")}),
        # G8: future-dated actual (effective 11:20) already visible at 10:05,
        # so the close-out finds a definite answer that is after the 11:00
        # close-out instant -> actual_arrival_after_close_out.
        "msg_g8_act.xml": _msg(
            "E-G8-ACT", "5008", eff=_ts("11:20"),
            times={"act_arr": _ts("11:20")}),
    }
    write_batch(os.path.join(base, "wh"), 4, AUTH, files_b4)

    # ---- Batch 5 (visible 10:30): close-out answers for G1/G2 and G5's late
    # actual.  G8's answer is intentionally NOT here (already in batch 4).
    files_b5 = {
        "msg_g1_act.xml": _msg(
            "E-G1-ACT", "5001", eff=_ts("10:20"),
            times={"act_arr": _ts("10:20")}),
        "msg_g2_act.xml": _msg(
            "E-G2-ACT", "5002", eff=_ts("10:25"),
            times={"act_arr": _ts("10:25")}),
        "msg_g5_act.xml": _msg(
            "E-G5-ACT", "5005", eff=_ts("10:48"),
            times={"act_arr": _ts("10:48")}),
        # G11's actual landing time 09:50 is only published now (10:30), well
        # after its 10:10 lead-30 checkpoint even though the plane had landed.
        "msg_g11_act.xml": _msg(
            "E-G11-ACT", "5011", eff=_ts("09:50"),
            times={"act_arr": _ts("09:50")}),
    }
    write_batch(os.path.join(base, "wh"), 5, AUTH, files_b5)


# ----------------------------------------------------------------- NDJSON in
def _roster_record(rid, root, group, sched):
    return {"record_id": rid, "type": "backtest_roster_row",
            "root_id": root, "group": group,
            "roster_sched_arr": _ts(sched)}


def build_inputs(base: str) -> None:
    in_dir = os.path.join(base, "inputs")
    os.makedirs(in_dir, exist_ok=True)

    # Roster: every operating root once, plus the never-declared GX.  Roster
    # scheduled times (frozen) intentionally differ for G4/G5 from anything the
    # warehouse later says, to prove checkpoints never move.
    roster = [
        _roster_record("RR-G1", "G1", "G1", "10:30"),
        _roster_record("RR-G2", "G2", "G1", "10:45"),
        _roster_record("RR-G9", "G9", "G1", "10:35"),
        _roster_record("RR-GX", "GX", "G1", "10:40"),
        _roster_record("RR-G3", "G3", "G2", "10:30"),
        _roster_record("RR-G4", "G4", "G2", "11:00"),
        _roster_record("RR-G5", "G5", "G2", "10:50"),
        _roster_record("RR-G10", "G10", "G2", "11:10"),
        _roster_record("RR-G6", "G6", "G3", "10:20"),
        _roster_record("RR-G11", "G11", "G3", "10:40"),
        _roster_record("RR-G12", "G12", "G2", "08:30"),
        _roster_record("RR-G7", "G7", "G3", "10:35"),
        _roster_record("RR-G8", "G8", "G3", "11:30"),
    ]
    with open(os.path.join(in_dir, "roster.ndjson"), "w",
              encoding="utf-8") as fh:
        for rec in roster:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    # Visibility: continuous prefix 1..5, non-decreasing instants, including a
    # tie-free monotone sequence; batch 2 is visible EXACTLY at 10:00.
    visibility = [
        (1, "08:00"), (2, "10:00"), (3, "10:02"), (4, "10:05"), (5, "10:30"),
    ]
    with open(os.path.join(in_dir, "visibility.ndjson"), "w",
              encoding="utf-8") as fh:
        for bno, hhmm in visibility:
            fh.write(json.dumps({
                "record_id": f"VB-{bno}", "type": "batch_visibility",
                "batch_no": bno,
                "first_visible_at": _ts(hhmm)}, sort_keys=True) + "\n")

    spec = {
        "airport": AIRPORT,
        "close_out_at": _ts("11:00"),
        "lead_minutes": [30, 60],
        "coverage_start": _ts("07:30"),
        "coverage_end": _ts("12:00"),
    }
    with open(os.path.join(in_dir, "spec.json"), "w",
              encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)


# ------------------------------------------------- hand-derived expectations
# Checkpoints (roster sched - lead), both leads 30 and 60:
#   G1 10:30 -> 10:00 (lead30), 09:30 (lead60)
#   G2 10:45 -> 10:15, 09:45
#   G9 10:35 -> 10:05, 09:35
#   GX 10:40 -> 10:10, 09:40
#   G3 10:30 -> 10:00, 09:30
#   G4 11:00 -> 10:30, 10:00
#   G5 10:50 -> 10:20, 09:50
#   G10 11:10 -> 10:40, 10:10
#   G6 10:20 -> 09:50, 09:20
#   G11 10:40 -> 10:10, 09:40
#   G12 08:30 -> 08:00, 07:30 (07:30: no batch visible yet)
#   G7 10:35 -> 10:05, 09:35
#   G8 11:30 -> 11:00, 10:30
#
# These literals are derived by hand below and checked independently in
# check_backtest.py, which never imports the backtest engine.
HAND_EXPECTED = {
    "airport": AIRPORT,
    "close_out_at": _ts("11:00"),
    "lead_minutes": [30, 60],
    "worked_example": {
        "note": "On the two scoreable rows G1 and G2 (10:00-style cut): "
                "G1 |est-act|=2 min and |sched-act|=10 min; G2 has its ETA "
                "cleared so only sched is available, |sched-act|=20 min. "
                "Scheduled coverage N=2, estimated coverage N=1, paired N=1. "
                "On the single paired sample the paired absolute-error "
                "difference (est minus sched) = 2-10 = -8 min "
                "= -480_000_000 us.  Subtracting the two one-sided MEAN "
                "absolute errors instead would compare different populations: "
                "2 - ((10+20)/2) = -13 min (-780_000_000 us), which is why "
                "the paired difference is the only valid comparison.",
        "G1": {"seen_est": "10:18", "seen_sched": "10:30",
               "close_actual": "10:20",
               "abs_est_error_min": 2, "abs_sched_error_min": 10},
        "G2": {"seen_est": "cleared", "seen_sched": "10:45",
               "close_actual": "10:25",
               "abs_est_error_min": None, "abs_sched_error_min": 20},
        "one_sided_means": {
            "sched_n": 2, "sched_mean_abs_minutes": 15.0,
            "est_n": 1, "est_mean_abs_minutes": 2.0,
            "naive_mean_difference_minutes": -13.0,
            "paired_abs_diff_est_minus_sched_minutes": -8.0,
        },
    },
}


def main(out: str | None = None) -> None:
    base = out or os.path.join(HERE, "..", "fixtures", "backtest")
    base = os.path.abspath(base)
    os.makedirs(base, exist_ok=True)
    build_warehouse(base)
    build_inputs(base)
    exp_dir = os.path.join(base, "expected")
    os.makedirs(exp_dir, exist_ok=True)
    with open(os.path.join(exp_dir, "hand_expected.json"), "w",
              encoding="utf-8") as fh:
        json.dump(HAND_EXPECTED, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"fixture_root": base,
                      "warehouse_batches": 5,
                      "roster_roots": 13, "lead_minutes": [30, 60]},
                     indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
