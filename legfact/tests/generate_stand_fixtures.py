#!/usr/bin/env python3
"""Deterministic fixtures for the offline stand-occupancy reconciliation.

Writes a small, hand-computable handover scenario into
``fixtures/stand/handover`` (three physical units A/B/C, a wide-body stand W
whose mapping swaps at 10:10 local, two turnarounds, observation conflicts,
adjacent online intervals with a late first observation, an offline record
and an observation exactly on the window right edge), plus:

* ``fixtures/stand/unmapped`` — a turnaround simultaneously declaring a mapped
  and an unregistered logical stand;
* ``fixtures/stand/invalid`` — structurally broken inputs that must fail the
  whole run.

The legfact warehouse batch is built with the warehouse's own synthetic AIDX
builders (no second flight-message dialect).

Run:  python3 -I tests/generate_stand_fixtures.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generate_fixtures import aidx_xml, identity_file, rel_initial, write_batch  # noqa: E402

BASE = os.path.abspath(os.path.join(HERE, "..", "fixtures", "stand"))
AIRPORT = "XYZ"
AUTH = "XYZ-OCC"
T0 = "2026-03-10T00:00:00+08:00"
# Audit window 09:00..11:00 local (+08:00); mapping swap at 10:10.
W_START = "2026-03-10T09:00:00+08:00"
W_END = "2026-03-10T11:00:00+08:00"
SWAP = "2026-03-10T10:10:00+08:00"
CUTOFF = "2026-03-10T12:00:00+08:00"


def key(fno: str, dep: str, arr: str) -> dict:
    return {"carrier": "CA", "flight_no": fno, "suffix": "",
            "service_date": "2026-03-10", "departure": dep, "arrival": arr}


def msg(event_id: str, k: dict, *, act_arr=None, act_dep=None,
        revision: int = 1, effective_at: str) -> tuple[str, str]:
    times = {}
    if act_arr is not None:
        times["act_arr"] = act_arr
    if act_dep is not None:
        times["act_dep"] = act_dep
    xml = aidx_xml(
        event_id=event_id, sender=AUTH, revision=revision,
        effective_at=effective_at, identity_at=T0,
        carrier=k["carrier"], flight_no=k["flight_no"], suffix="",
        service_date=k["service_date"], dep=k["departure"], arr=k["arrival"],
        times=times)
    return xml, "aidx_xml"


def write_ndjson(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True))
            fh.write("\n")


def visit(rid, vid, arr_root, dep_root) -> dict:
    return {"record_id": rid, "type": "stand_visit", "visit_id": vid,
            "arrival_root": arr_root, "departure_root": dep_root}


def usage(rid, vid, stand, start, end) -> dict:
    return {"record_id": rid, "type": "stand_usage", "record_id": rid,
            "visit_id": vid, "stand": stand, "start": start, "end": end} \
        if False else {"record_id": rid, "type": "stand_usage",
                       "visit_id": vid, "stand": stand,
                       "start": start, "end": end}


def unit(rid, uid) -> dict:
    return {"record_id": rid, "type": "physical_unit", "unit_id": uid}


def map_version(rid, vid, start, end, mapping) -> dict:
    return {"record_id": rid, "type": "stand_map_version",
            "version_id": vid, "start": start, "end": end, "mapping": mapping}


def online(rid, uid, start, end) -> dict:
    return {"record_id": rid, "type": "online_interval", "unit_id": uid,
            "start": start, "end": end}


def obs(rid, uid, at, state) -> dict:
    return {"record_id": rid, "type": "observation", "unit_id": uid,
            "at": at, "state": state}


# --------------------------------------------------------------- warehouse
def build_warehouse(root: str) -> None:
    # V1: inbound CA201 PEK->XYZ, outbound CA202 XYZ->PEK.
    k201 = key("201", "PEK", AIRPORT)
    k202 = key("202", AIRPORT, "PEK")
    # V2: inbound CA301 CAN->XYZ, outbound CA302 XYZ->CAN.
    k301 = key("301", "CAN", AIRPORT)
    k302 = key("302", AIRPORT, "CAN")
    rels = [
        rel_initial("IL-201", "R201", k201, T0, AUTH, "J-V1"),
        rel_initial("IL-202", "R202", k202, T0, AUTH, "J-V1"),
        rel_initial("IL-301", "R301", k301, T0, AUTH, "J-V2"),
        rel_initial("IL-302", "R302", k302, T0, AUTH, "J-V2"),
    ]
    files = {
        "identity_b1.json": (identity_file(rels), "identity_json"),
        # V1 on ground 09:20 .. 10:40 (actual), shorter than its W claim.
        "msg_201.xml": msg("E-201", k201,
                           act_arr="2026-03-10T09:20:00+08:00",
                           effective_at="2026-03-10T09:25:00+08:00"),
        "msg_202.xml": msg("E-202", k202,
                           act_dep="2026-03-10T10:40:00+08:00",
                           effective_at="2026-03-10T10:45:00+08:00"),
        # V2 on ground 09:40 .. 10:05.
        "msg_301.xml": msg("E-301", k301,
                           act_arr="2026-03-10T09:40:00+08:00",
                           effective_at="2026-03-10T09:45:00+08:00"),
        "msg_302.xml": msg("E-302", k302,
                           act_dep="2026-03-10T10:05:00+08:00",
                           effective_at="2026-03-10T10:10:00+08:00"),
    }
    write_batch(root, 1, AUTH, files)


# ------------------------------------------------------------------ inputs
def build_inputs(root: str) -> None:
    roster = [
        visit("VR-1", "V1", "R201", "R202"),
        visit("VR-2", "V2", "R301", "R302"),
    ]
    write_ndjson(os.path.join(root, "visits.ndjson"), roster)

    ledger = [
        # V1: one W declaration spanning the 10:10 remap; its declared end
        # (10:50) is longer than the real ground interval (ends 10:40).
        usage("LR-1", "V1", "W", "2026-03-10T09:25:00+08:00",
              "2026-03-10T10:50:00+08:00"),
        # V1 after the swap: W and S declared together 10:25..10:35.
        usage("LR-2", "V1", "W", "2026-03-10T10:25:00+08:00",
              "2026-03-10T10:35:00+08:00"),
        usage("LR-3", "V1", "S", "2026-03-10T10:25:00+08:00",
              "2026-03-10T10:35:00+08:00"),
        # V2 pre-swap: narrow-body S only.
        usage("LR-4", "V2", "S", "2026-03-10T09:40:00+08:00",
              "2026-03-10T10:05:00+08:00"),
        # V2 post-swap: W (coexists with V1's long W on shared unit B).
        usage("LR-5", "V2", "W", "2026-03-10T10:15:00+08:00",
              "2026-03-10T10:20:00+08:00"),
        # A retained row that never intersects any ground interval.
        usage("LR-6", "V2", "S", "2026-03-10T11:30:00+08:00",
              "2026-03-10T11:40:00+08:00"),
        # A retained row naming a visit absent from the roster.
        usage("LR-7", "V-GHOST", "S", "2026-03-10T09:30:00+08:00",
              "2026-03-10T09:35:00+08:00"),
    ]
    write_ndjson(os.path.join(root, "ledger.ndjson"), ledger)

    stand_map = [
        unit("UR-A", "A"), unit("UR-B", "B"), unit("UR-C", "C"),
        map_version("MR-1", "MAP-1", "2026-03-10T00:00:00+08:00", SWAP,
                    {"W": ["A", "B"], "S": ["B"]}),
        map_version("MR-2", "MAP-2", SWAP,
                    "2026-03-11T00:00:00+08:00",
                    {"W": ["B", "C"], "S": ["B"]}),
    ]
    write_ndjson(os.path.join(root, "map.ndjson"), stand_map)

    sensor = [
        # ---- B: pre-window observation exists; same-instant conflict at
        # 09:10; two adjacent online intervals, the second with a late first
        # observation; an observation exactly on the right edge (11:00).
        online("ON-B1", "B", "2026-03-10T08:30:00+08:00",
               "2026-03-10T10:20:00+08:00"),
        online("ON-B2", "B", "2026-03-10T10:20:00+08:00",
               "2026-03-10T11:30:00+08:00"),
        obs("OB-B0", "B", "2026-03-10T08:45:00+08:00", "idle"),
        obs("OB-B1", "B", "2026-03-10T09:10:00+08:00", "idle"),
        obs("OB-B2", "B", "2026-03-10T09:10:00+08:00", "occupied"),
        obs("OB-B3", "B", "2026-03-10T09:30:00+08:00", "occupied"),
        # No observation at the 10:20 restart: the second online interval
        # stays unknown until its first observation arrives at 10:40.
        obs("OB-B4", "B", "2026-03-10T10:40:00+08:00", "idle"),
        obs("OB-B5", "B", "2026-03-10T11:00:00+08:00", "occupied"),
        # ---- A: idle -> occupied -> idle, covering V1's pre-swap W time.
        online("ON-A1", "A", "2026-03-10T08:00:00+08:00",
               "2026-03-10T11:30:00+08:00"),
        obs("OB-A1", "A", "2026-03-10T08:50:00+08:00", "idle"),
        obs("OB-A2", "A", "2026-03-10T09:30:00+08:00", "occupied"),
        obs("OB-A3", "A", "2026-03-10T10:05:00+08:00", "idle"),
        # ---- C: post-swap unit; includes an offline observation and a
        # window-right-edge group that must not change in-window state.
        online("ON-C1", "C", "2026-03-10T08:00:00+08:00",
               "2026-03-10T10:35:00+08:00"),
        online("ON-C2", "C", "2026-03-10T10:35:00+08:00",
               "2026-03-10T10:50:00+08:00"),
        obs("OB-C1", "C", "2026-03-10T09:30:00+08:00", "idle"),
        obs("OB-C2", "C", "2026-03-10T10:12:00+08:00", "occupied"),
        obs("OB-C3", "C", "2026-03-10T10:45:00+08:00", "occupied"),
        # 11:10 is outside every online interval (the sensor went offline at
        # 10:50): retained as an offline record, producing no state.
        obs("OB-COFF", "C", "2026-03-10T11:10:00+08:00", "occupied"),
        # Exactly-on-window-end group (exclusive edge): never changes the
        # in-window result.
        obs("OB-C4", "C", "2026-03-10T11:00:00+08:00", "idle"),
        obs("OB-C5", "C", "2026-03-10T11:00:00+08:00", "occupied"),
    ]
    write_ndjson(os.path.join(root, "sensor.ndjson"), sensor)

    with open(os.path.join(root, "run_params.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"airport": AIRPORT, "window_start": W_START,
                   "window_end": W_END, "receipt_batch": 1,
                   "cutoff": CUTOFF}, fh, indent=2, sort_keys=True)


# --------------------------------------------- unmapped-second-stand fixture
def build_unmapped(root: str) -> None:
    # Same warehouse roots; V1 simultaneously declares W (mapped) and Z (no
    # mapping).  Z must trigger a multi-stand conflict: W must never be
    # reported as a unique attribution.
    write_ndjson(os.path.join(root, "visits.ndjson"), [
        visit("VR-1", "V1", "R201", "R202")])
    write_ndjson(os.path.join(root, "ledger.ndjson"), [
        usage("LR-1", "V1", "W", "2026-03-10T09:30:00+08:00",
              "2026-03-10T10:00:00+08:00"),
        usage("LR-2", "V1", "Z", "2026-03-10T09:30:00+08:00",
              "2026-03-10T10:00:00+08:00"),
    ])
    write_ndjson(os.path.join(root, "map.ndjson"), [
        unit("UR-A", "A"), unit("UR-B", "B"), unit("UR-C", "C"),
        map_version("MR-1", "MAP-1", "2026-03-10T00:00:00+08:00",
                    "2026-03-11T00:00:00+08:00",
                    {"W": ["A", "B"], "S": ["B"]}),
    ])
    write_ndjson(os.path.join(root, "sensor.ndjson"), [
        online("ON-A1", "A", "2026-03-10T08:00:00+08:00",
               "2026-03-10T11:30:00+08:00"),
        online("ON-B1", "B", "2026-03-10T08:00:00+08:00",
               "2026-03-10T11:30:00+08:00"),
        obs("OA1", "A", "2026-03-10T08:30:00+08:00", "occupied"),
        obs("OB1", "B", "2026-03-10T08:30:00+08:00", "occupied"),
    ])


# --------------------------------------------------------- invalid fixtures
def build_invalid(base: str) -> None:
    good_map = [unit("UR-A", "A"), unit("UR-B", "B"),
                map_version("MR-1", "MAP-1",
                            "2026-03-10T00:00:00+08:00",
                            "2026-03-11T00:00:00+08:00",
                            {"W": ["A"], "S": ["B"]})]
    good_sensor = [online("ON-1", "A", "2026-03-10T08:00:00+08:00",
                          "2026-03-10T11:30:00+08:00"),
                   obs("OB-1", "A", "2026-03-10T09:30:00+08:00", "occupied")]
    good_visits = [visit("VR-1", "V1", "R201", "R202")]
    good_ledger = [usage("LR-1", "V1", "W",
                         "2026-03-10T09:30:00+08:00",
                         "2026-03-10T10:00:00+08:00")]

    def write_case(name, *, visits=None, ledger=None, mp=None, sensor=None,
                   raw=None):
        d = os.path.join(base, name)
        if raw is not None:
            for fname, content in raw.items():
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                    fh.write(content)
            return
        write_ndjson(os.path.join(d, "visits.ndjson"),
                     good_visits if visits is None else visits)
        write_ndjson(os.path.join(d, "ledger.ndjson"),
                     good_ledger if ledger is None else ledger)
        write_ndjson(os.path.join(d, "map.ndjson"),
                     good_map if mp is None else mp)
        write_ndjson(os.path.join(d, "sensor.ndjson"),
                     good_sensor if sensor is None else sensor)

    # malformed JSON line
    write_case("malformed", raw={"visits.ndjson": '{"not json\n'})
    # duplicate primary key (visit_id)
    write_case("dup_visit", visits=good_visits + [
        visit("VR-2", "V1", "R301", "R302")])
    # root reused in the same role
    write_case("root_reuse", visits=good_visits + [
        visit("VR-2", "V2", "R201", "R302")])
    # overlapping map versions
    write_case("overlap_map", mp=[
        unit("UR-A", "A"), unit("UR-B", "B"),
        map_version("MR-1", "MAP-1", "2026-03-10T00:00:00+08:00",
                    "2026-03-10T10:00:00+08:00", {"W": ["A"], "S": ["B"]}),
        map_version("MR-2", "MAP-2", "2026-03-10T09:00:00+08:00",
                    "2026-03-11T00:00:00+08:00", {"W": ["B"], "S": ["B"]})])
    # map gap over the window
    write_case("gap_map", mp=[
        unit("UR-A", "A"), unit("UR-B", "B"),
        map_version("MR-1", "MAP-1", "2026-03-10T00:00:00+08:00",
                    "2026-03-10T10:00:00+08:00", {"W": ["A"], "S": ["B"]}),
        map_version("MR-2", "MAP-2", "2026-03-10T10:30:00+08:00",
                    "2026-03-11T00:00:00+08:00", {"W": ["B"], "S": ["B"]})])
    # overlapping online intervals on the same unit
    write_case("overlap_online", sensor=[
        online("ON-1", "A", "2026-03-10T08:00:00+08:00",
               "2026-03-10T10:00:00+08:00"),
        online("ON-2", "A", "2026-03-10T09:00:00+08:00",
               "2026-03-10T11:00:00+08:00"),
        obs("OB-1", "A", "2026-03-10T09:30:00+08:00", "occupied")])
    # bad interval (end <= start)
    write_case("bad_interval", sensor=[
        online("ON-1", "A", "2026-03-10T10:00:00+08:00",
               "2026-03-10T09:00:00+08:00")])


def main() -> None:
    handover = os.path.join(BASE, "handover")
    build_warehouse(os.path.join(handover, "wh"))
    build_inputs(os.path.join(handover, "inputs"))
    build_unmapped(os.path.join(BASE, "unmapped"))
    build_invalid(os.path.join(BASE, "invalid"))
    print("stand fixtures written under", BASE)


if __name__ == "__main__":
    main()
