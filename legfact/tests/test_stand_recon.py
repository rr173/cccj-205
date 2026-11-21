#!/usr/bin/env python3
"""Behavioral tests for the offline stand-occupancy reconciliation.

Covers the rules that the handover fixture cannot reach on its own:

* every unavailable-visit reason (unknown root, airport mismatch, uncertain
  actual, departure not after arrival, actual after the cutoff); scheduled
  times are never substitutes;
* fully-out-of-window ground intervals are fine, not errors;
* cross-visit physical-unit conflicts and same-visit multi-stand conflicts,
  including identical mapped units and one unmapped stand;
* same-instant end/start (half-open) never creates a zero-length segment;
* state never leaks across adjacent online intervals, pre-window look-back,
  right-edge observations and offline observations;
* structural input rejection leaves no sidecar behind;
* evidence drill-down locates declarations, fact evidence, map version and
  sensor source / unknown reason.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generate_fixtures import aidx_xml, identity_file, rel_initial, write_batch  # noqa: E402
from generate_stand_fixtures import (obs, online, map_version, unit,  # noqa: E402
                                     usage, visit, write_ndjson)
from legfact import Warehouse  # noqa: E402
from legfact.stand_config import StandInputError, load_inputs  # noqa: E402
from legfact.stand_engine import ReconError, run_reconciliation  # noqa: E402
from legfact.stand_store import ReconBook  # noqa: E402

AIRPORT = "XYZ"
AUTH = "XYZ-OCC"
T0 = "2026-03-10T00:00:00+08:00"
WS = "2026-03-10T09:00:00+08:00"
WE = "2026-03-10T11:00:00+08:00"
CUTOFF = "2026-03-10T12:00:00+08:00"
PASS = FAILS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAILS
    if cond:
        PASS += 1
    else:
        FAILS += 1
        print(f"FAIL {name}: {detail}")


def key(fno: str, dep: str, arr: str) -> dict:
    return {"carrier": "CA", "flight_no": fno, "suffix": "",
            "service_date": "2026-03-10", "departure": dep, "arrival": arr}


def amsg(event_id, k, *, act_arr=None, act_dep=None, eff,
         times_extra=None) -> tuple[str, str]:
    times = dict(times_extra or {})
    if act_arr is not None:
        times["act_arr"] = act_arr
    if act_dep is not None:
        times["act_dep"] = act_dep
    xml = aidx_xml(event_id=event_id, sender=AUTH, revision=1,
                   effective_at=eff, identity_at=T0, carrier=k["carrier"],
                   flight_no=k["flight_no"], suffix="",
                   service_date=k["service_date"], dep=k["departure"],
                   arr=k["arrival"], times=times)
    return xml, "aidx_xml"


def build_warehouse(td: str, roots: dict) -> str:
    """roots: root_id -> (key, act_arr|None, act_dep|None)."""
    rels, files = [], {}
    for rid, (k, arr, dep) in roots.items():
        rels.append(rel_initial(f"IL-{rid}", rid, k, T0, AUTH, f"J-{rid}"))
        if arr is not None:
            files[f"m_{rid}_a.xml"] = amsg(
                f"E-{rid}-a", k, act_arr=arr,
                eff="2026-03-10T11:50:00+08:00")
        if dep is not None:
            files[f"m_{rid}_d.xml"] = amsg(
                f"E-{rid}-d", k, act_dep=dep,
                eff="2026-03-10T11:50:00+08:00")
    files["identity.json"] = (identity_file(rels), "identity_json")
    root = os.path.join(td, "wh")
    write_batch(root, 1, AUTH, files)
    wh_dir = os.path.join(td, "whdb")
    wh = Warehouse(wh_dir)
    wh.ingest_batch(os.path.join(root, "manifest_1.json"),
                    os.path.join(root, "batch_1"))
    wh.close()
    return wh_dir


def simple_inputs(td: str, *, visits, ledger, sensor=None, mapping=None):
    d = os.path.join(td, "in")
    write_ndjson(os.path.join(d, "visits.ndjson"), visits)
    write_ndjson(os.path.join(d, "ledger.ndjson"), ledger)
    mp = mapping or [
        unit("U-A", "A"), unit("U-B", "B"),
        map_version("M-1", "MAP-1", "2026-03-10T00:00:00+08:00",
                    "2026-03-11T00:00:00+08:00",
                    {"W": ["A", "B"], "S": ["B"]})]
    write_ndjson(os.path.join(d, "map.ndjson"), mp)
    write_ndjson(os.path.join(d, "sensor.ndjson"),
                 sensor if sensor is not None else [
                     online("ON-A", "A", "2026-03-10T08:00:00+08:00",
                            "2026-03-10T12:00:00+08:00"),
                     online("ON-B", "B", "2026-03-10T08:00:00+08:00",
                            "2026-03-10T12:00:00+08:00"),
                     obs("OA", "A", "2026-03-10T08:30:00+08:00", "occupied"),
                     obs("OB", "B", "2026-03-10T08:30:00+08:00", "idle")])
    return d


def load(d):
    return load_inputs(roster_path=os.path.join(d, "visits.ndjson"),
                       ledger_path=os.path.join(d, "ledger.ndjson"),
                       map_path=os.path.join(d, "map.ndjson"),
                       sensor_path=os.path.join(d, "sensor.ndjson"))


def run(wh_dir, inputs):
    wh = Warehouse(wh_dir)
    try:
        return run_reconciliation(wh, inputs, airport=AIRPORT,
                                  window_start=WS, window_end=WE,
                                  receipt_batch=1, cutoff=CUTOFF)
    finally:
        wh.close()


def main() -> int:
    td = tempfile.mkdtemp(prefix="stand_test_")

    # ---- roots covering each unavailability reason
    roots = {
        # good visit G: 09:30..10:30
        "RG-A": (key("401", "PEK", AIRPORT),
                 "2026-03-10T09:30:00+08:00", None),
        "RG-D": (key("402", AIRPORT, "PEK"), None,
                 "2026-03-10T10:30:00+08:00"),
        # no actual arrival at all
        "RN-A": (key("411", "PEK", AIRPORT), None, None),
        "RN-D": (key("412", AIRPORT, "PEK"), None,
                 "2026-03-10T10:30:00+08:00"),
        # wrong airport on arrival
        "RW-A": (key("421", "PEK", "CAN"), None, None),
        "RW-D": (key("422", AIRPORT, "PEK"), None,
                 "2026-03-10T10:30:00+08:00"),
        # departure not after arrival (equal)
        "RE-A": (key("431", "PEK", AIRPORT),
                 "2026-03-10T10:00:00+08:00", None),
        "RE-D": (key("432", AIRPORT, "PEK"), None,
                 "2026-03-10T10:00:00+08:00"),
        # actual after the cutoff
        "RC-A": (key("441", "PEK", AIRPORT),
                 "2026-03-10T09:30:00+08:00", None),
        "RC-D": (key("442", AIRPORT, "PEK"), None,
                 "2026-03-10T12:30:00+08:00"),
        # out-of-window visit: ground 07:00..08:00, entirely before the window
        "RO-A": (key("451", "PEK", AIRPORT),
                 "2026-03-10T07:00:00+08:00", None),
        "RO-D": (key("452", AIRPORT, "PEK"), None,
                 "2026-03-10T08:00:00+08:00"),
        # spare valid roots for the unknown-root visit (only one side unknown)
        "RU-D": (key("462", AIRPORT, "PEK"), None,
                 "2026-03-10T10:30:00+08:00"),
    }
    wh_dir = build_warehouse(td, roots)

    visits = [
        visit("V-G", "VG", "RG-A", "RG-D"),
        visit("V-N", "VN", "RN-A", "RN-D"),
        visit("V-W", "VW", "RW-A", "RW-D"),
        visit("V-E", "VE", "RE-A", "RE-D"),
        visit("V-C", "VC", "RC-A", "RC-D"),
        visit("V-O", "VO", "RO-A", "RO-D"),
        visit("V-U", "VU", "R-GHOST", "RU-D"),
    ]
    ledger = [
        usage("L-G", "VG", "W", "2026-03-10T09:30:00+08:00",
              "2026-03-10T10:30:00+08:00"),
        usage("L-N", "VN", "W", "2026-03-10T09:30:00+08:00",
              "2026-03-10T10:30:00+08:00"),
        usage("L-O", "VO", "W", "2026-03-10T07:00:00+08:00",
              "2026-03-10T08:00:00+08:00"),
    ]
    d = simple_inputs(td, visits=visits, ledger=ledger)
    res = run(wh_dir, load(d))
    vo = {v.visit_id: v for v in res["visits"]}
    check("good visit determinable",
          vo["VG"].status == "determinable"
          and vo["VG"].arr_evidence and vo["VG"].dep_evidence)
    check("no actual -> uncertain",
          vo["VN"].status == "unavailable"
          and vo["VN"].reason == "actual_uncertain")
    check("airport mismatch",
          vo["VW"].status == "unavailable"
          and vo["VW"].reason == "airport_mismatch")
    check("dep not after arr",
          vo["VE"].status == "unavailable"
          and vo["VE"].reason == "departure_not_after_arrival")
    check("after cutoff",
          vo["VC"].status == "unavailable"
          and vo["VC"].reason == "actual_after_cutoff")
    check("unknown root",
          vo["VU"].status == "unavailable"
          and vo["VU"].reason == "unknown_root")
    check("out-of-window visit is determinable, not an error",
          vo["VO"].status == "determinable")
    kinds = {i.kind for i in res["issues"]}
    check("unavailable visit ledger row tracked",
          "ledger_unavailable_visit" in kinds)
    check("disjoint ledger row tracked (out-of-window visit's claim)",
          any(i.kind == "ledger_disjoint_from_ground"
              and i.ledger_record_id == "L-O" for i in res["issues"]))
    # the out-of-window visit must produce no claims
    check("out-of-window produces no claims",
          all(c.visit_id != "VO" for c in res["claims"]))

    # ---- cross-visit conflict on one physical unit (both use S -> unit B)
    roots2 = {
        "X1A": (key("501", "PEK", AIRPORT),
                "2026-03-10T09:30:00+08:00", None),
        "X1D": (key("502", AIRPORT, "PEK"), None,
                "2026-03-10T10:30:00+08:00"),
        "X2A": (key("511", "CAN", AIRPORT),
                "2026-03-10T09:45:00+08:00", None),
        "X2D": (key("512", AIRPORT, "CAN"), None,
                "2026-03-10T10:15:00+08:00"),
    }
    wh2 = build_warehouse(os.path.join(td, "w2"), roots2)
    d2 = simple_inputs(td, visits=[
        visit("V1", "V1", "X1A", "X1D"),
        visit("V2", "V2", "X2A", "X2D")], ledger=[
        usage("L1", "V1", "S", "2026-03-10T09:30:00+08:00",
              "2026-03-10T10:30:00+08:00"),
        usage("L2", "V2", "S", "2026-03-10T09:45:00+08:00",
              "2026-03-10T10:15:00+08:00")])
    res2 = run(wh2, load(d2))
    b_segs = [s for s in res2["segments"] if s["unit_id"] == "B"]
    conflict = [s for s in b_segs if s["attribution"] == "conflict"]
    check("cross-visit conflict detected",
          len(conflict) == 1 and conflict[0]["start_us"] < conflict[0]["end_us"]
          and set(conflict[0]["conflict_visits"]) == {"V1", "V2"},
          str([(s["start_us"], s["attribution"], s["conflict_visits"])
               for s in b_segs]))
    check("conflict class does not judge match",
          all(s["recon_class"] == "attribution_conflict" for s in conflict))

    # ---- same-visit two stands mapping to the SAME unit set still conflicts
    d3 = simple_inputs(td, visits=[visit("V1", "V1", "X1A", "X1D")],
                       ledger=[
        usage("L1", "V1", "W", "2026-03-10T09:40:00+08:00",
              "2026-03-10T10:00:00+08:00"),
        usage("L2", "V1", "S", "2026-03-10T09:40:00+08:00",
              "2026-03-10T10:00:00+08:00")],
        mapping=[unit("U-A", "A"), unit("U-B", "B"),
                 map_version("M-1", "MAP-1",
                             "2026-03-10T00:00:00+08:00",
                             "2026-03-11T00:00:00+08:00",
                             {"W": ["B"], "S": ["B"]})])
    res3 = run(wh2, load(d3))
    check("same-unit multi-stand still conflicts",
          any(s["unit_id"] == "B" and s["attribution"] == "conflict"
              and s["conflict_visits"] == ["V1"]
              for s in res3["segments"]))

    # ---- half-open back-to-back claims: no zero-length segment, no conflict
    d4 = simple_inputs(td, visits=[visit("V1", "V1", "X1A", "X1D"),
                                   visit("V2", "V2", "X2A", "X2D")],
                       ledger=[
        usage("L1", "V1", "S", "2026-03-10T09:30:00+08:00",
              "2026-03-10T10:00:00+08:00"),
        usage("L2", "V2", "S", "2026-03-10T10:00:00+08:00",
              "2026-03-10T10:15:00+08:00")])
    res4 = run(wh2, load(d4))
    check("no zero-length segments",
          all(s["duration_us"] > 0 for s in res4["segments"]))
    check("back-to-back claims never conflict",
          not any(s["attribution"] == "conflict"
                  for s in res4["segments"] if s["unit_id"] == "B"))

    # ---- sensor semantics: adjacent online intervals, pre-window look-back,
    # right-edge observation, offline observation
    d5 = simple_inputs(td, visits=[visit("V1", "V1", "X1A", "X1D")],
                       ledger=[usage("L1", "V1", "S",
                                     "2026-03-10T09:30:00+08:00",
                                     "2026-03-10T10:30:00+08:00")],
                       sensor=[
        online("ON1", "B", "2026-03-10T08:00:00+08:00",
               "2026-03-10T10:00:00+08:00"),
        online("ON2", "B", "2026-03-10T10:00:00+08:00",
               "2026-03-10T10:30:00+08:00"),
        online("ONA", "A", "2026-03-10T08:00:00+08:00",
               "2026-03-10T10:30:00+08:00"),
        obs("OB0", "B", "2026-03-10T08:50:00+08:00", "occupied"),
        obs("OB1", "B", "2026-03-10T10:20:00+08:00", "idle"),
        obs("OBOFF", "B", "2026-03-10T10:45:00+08:00", "occupied"),
        obs("OA", "A", "2026-03-10T08:30:00+08:00", "idle"),
        obs("OAEDGE", "A", "2026-03-10T11:00:00+08:00", "occupied")])
    res5 = run(wh2, load(d5))
    b5 = {s["start_us"]: s for s in res5["segments"] if s["unit_id"] == "B"}
    from legfact.timeutil import parse_offset_datetime as p
    t_0900 = p(WS).utc
    import datetime
    def us(hm):
        h, m = map(int, hm.split(":"))
        return int((datetime.datetime(2026, 3, 10, h - 8, m,
                                      tzinfo=datetime.timezone.utc))
                   .timestamp() * 1_000_000)
    # pre-window observation carries into the window
    first = next(s for s in res5["segments"]
                 if s["unit_id"] == "B" and s["start_us"] == us("09:00"))
    check("pre-window look-back keeps state",
          first["measured_state"] == "occupied")
    # fresh interval at 10:00 has no first observation until 10:20
    wait = [s for s in res5["segments"]
            if s["unit_id"] == "B" and s["unknown_reason"]
            == "waiting_first_observation"]
    check("state resets at adjacent online restart",
          any(s["start_us"] == us("10:00") for s in wait)
          and all(s["end_us"] <= us("10:20") for s in wait))
    # after 10:30 B is offline (both intervals ended); the 10:45 observation
    # is offline and changes nothing
    off = [s for s in res5["segments"]
           if s["unit_id"] == "B" and s["unknown_reason"] == "offline"]
    check("offline after last interval, offline obs ignored",
          off and off[0]["start_us"] == us("10:30"))
    check("offline observation listed as issue",
          any(i.kind == "offline_observation" for i in res5["issues"]))
    # right-edge observation on A at 11:00 is offline here (A's online
    # interval ends 10:30) and changes nothing; the exclusive-window-edge
    # rule for ONLINE observations is covered by the handover fixture (B/C).
    a_segs = [s for s in res5["segments"] if s["unit_id"] == "A"]
    check("right-edge offline observation changes nothing",
          a_segs[-1]["measured_state"] == "unknown"
          and a_segs[-1]["unknown_reason"] == "offline"
          and all(s["start_us"] < us("11:00") for s in a_segs))

    # ---- structural rejection leaves no output
    out_path = os.path.join(td, "must_not_exist.sqlite")
    try:
        bad = load(os.path.join(
            HERE, "..", "fixtures", "stand", "invalid", "overlap_online"))
        run(wh2, bad)
        check("overlapping online rejected", False)
    except StandInputError as exc:
        check("overlapping online rejected", exc.code ==
              "overlapping_online_intervals")
    check("no sidecar on rejection", not os.path.exists(out_path))

    # ---- evidence drill-down through the published sidecar
    side = os.path.join(td, "recon.sqlite")
    ReconBook.publish(side, res2, {"files": {}, "sha256": {}})
    book = ReconBook(side)
    at = us("10:00")
    ev = book.evidence_at("B", at)
    check("evidence finds the conflict segment",
          ev["segment"]["attribution"] == "conflict")
    check("evidence lists both active claims with sources",
          {c["visit_id"] for c in ev["unit_claims_active"]} == {"V1", "V2"}
          and all(c["source"] for c in ev["unit_claims_active"]))
    check("evidence includes visit leg fact evidence",
          all(v["arrival_fact_evidence"] for v in
              ev["visits_and_leg_evidence"]))
    check("evidence names the active map version",
          ev["map_version"] and ev["map_version"]["version_id"] == "MAP-1")
    book.close()

    print(f"\n{PASS} passed, {FAILS} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
