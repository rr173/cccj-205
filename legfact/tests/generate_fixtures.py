#!/usr/bin/env python3
"""Deterministic fixture generator for the leg-fact warehouse.

All fixtures are synthetic (the warehouse's own synthetic AIDX profile).
The generator writes:

  fixtures/main/      handover batches 1..3 with manifests
  fixtures/edge/      targeted edge-case batches
  fixtures/large/     a large-payload batch for memory measurement
  fixtures/expected/  hand-written expectation tables (NOT produced by the
                      projection code — see expected_walkthrough.md / .json)

Run:  python3 tests/generate_fixtures.py [out_dir]
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact.manifest import build_manifest, write_manifest  # noqa: E402

NS = 'xmlns="urn:org:aidx:synth:legfact:1"'
AUTH = "CA-OCC"          # authoritative sender for roots L, M, N
OTHER = "ZZ-GROUND"      # non-authoritative sender -> external evidence
UNKNOWN = "XX-STRANGER"  # unknown sender (no root declares it)


def aidx_xml(*, event_id: str, sender: str, revision: int,
             effective_at: str, identity_at: str, mtype: str = "FlightUpdate",
             carrier: str = "CA", flight_no: str = "100", suffix: str = "",
             service_date: str = "2026-11-20", dep: str = "PEK", arr: str = "SHA",
             times: dict | None = None, gates: dict | None = None,
             cancellation: dict | None = None,
             unsupported: str | None = None,
             nil_times: tuple = (), bad_offset: bool = False) -> str:
    times = times or {}
    gates = gates or {}
    suffix_xml = (f"      <Suffix>{suffix}</Suffix>\n" if suffix else
                  "      <Suffix nil=\"true\"></Suffix>\n")
    time_order = ("sched_dep", "sched_arr", "est_dep", "est_arr",
                  "act_dep", "act_arr")
    tag_map = {
        "sched_dep": "ScheduledTimeOffBlocks", "sched_arr": "ScheduledTimeOnBlocks",
        "est_dep": "EstimatedTimeOffBlocks", "est_arr": "EstimatedTimeOnBlocks",
        "act_dep": "ActualTimeOffBlocks", "act_arr": "ActualTimeOnBlocks",
    }
    time_lines = []
    for f in time_order:
        if f in nil_times:
            time_lines.append(f"      <{tag_map[f]} nil=\"true\"></{tag_map[f]}>")
        elif f in times:
            val = times[f]
            if bad_offset:
                val = val.replace("+08:00", "").replace("Z", "")
            time_lines.append(f"      <{tag_map[f]}>{val}</{tag_map[f]}>")
    gate_lines = []
    if "dep_gate" in gates:
        g = gates["dep_gate"]
        if g is None:
            gate_lines.append("      <Departure><Gate code=\"\" nil=\"true\"></Gate></Departure>")
        else:
            gate_lines.append(f"      <Departure><Gate code=\"{g}\"></Gate></Departure>")
    if "arr_gate" in gates:
        g = gates["arr_gate"]
        if g is None:
            gate_lines.append("      <Arrival><Gate code=\"NONE\"></Gate></Arrival>")
        else:
            gate_lines.append(f"      <Arrival><Gate code=\"{g}\"></Gate></Arrival>")
    cancel_xml = ""
    if cancellation:
        j = f" journey_id=\"{cancellation['journey_id']}\"" if cancellation.get("journey_id") else ""
        cancel_xml = (f"   <Cancellation scope=\"{cancellation['scope']}\" "
                      f"action=\"{cancellation['action']}\"{j}></Cancellation>\n")
    unsup_xml = f"      <BookingReference>{unsupported}</BookingReference>\n" if unsupported else ""
    return (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<AIDXMessageEnvelope {NS}>\n"
        f"   <EventID>{event_id}</EventID>\n"
        f"   <Sender>{sender}</Sender>\n"
        f"   <SourceRevision>{revision}</SourceRevision>\n"
        f"   <EffectiveAt>{effective_at}</EffectiveAt>\n"
        f"   <IdentityAt>{identity_at}</IdentityAt>\n"
        f"   <MessageType>{mtype}</MessageType>\n"
        f"   <FlightLeg>\n"
        f"      <OperatingCarrier>{carrier}</OperatingCarrier>\n"
        f"      <FlightNumber>{flight_no}</FlightNumber>\n"
        f"{suffix_xml}"
        f"      <ServiceDate>{service_date}</ServiceDate>\n"
        f"      <DepartureStation>{dep}</DepartureStation>\n"
        f"      <ArrivalStation>{arr}</ArrivalStation>\n"
        + "\n".join(time_lines)
        + ("\n" if time_lines else "")
        + "\n".join(gate_lines)
        + ("\n" if gate_lines else "")
        + unsup_xml
        + f"   </FlightLeg>\n"
        + cancel_xml
        + f"</AIDXMessageEnvelope>\n"
    )


def identity_file(relations: list[dict]) -> str:
    return json.dumps({"relations": relations}, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------- keys
# Root L: CA100 (suffix empty), 2026-11-20, PEK->SHA, journey J1
L_OLD = dict(carrier="CA", flight_no="100", suffix="",
             service_date="2026-11-20", departure="PEK", arrival="SHA")
# renamed at 10:00 to CA200
L_NEW = dict(L_OLD, flight_no="200")
# Root M: CA300 same journey J1
M_KEY = dict(carrier="CA", flight_no="300", suffix="",
             service_date="2026-11-20", departure="SHA", arrival="CAN")
# Root N: CA100 on a DIFFERENT operating day, journey J2 (must never bleed in)
N_KEY = dict(carrier="CA", flight_no="100", suffix="",
             service_date="2026-11-21", departure="PEK", arrival="SHA")
# Marketing codeshare key (KA1000) onto L, and separately onto N
MK_L = dict(carrier="KA", flight_no="1000", suffix="",
            service_date="2026-11-20", departure="PEK", arrival="SHA")
MK_N = dict(carrier="KA", flight_no="1000", suffix="",
            service_date="2026-11-21", departure="PEK", arrival="SHA")


def rel_initial(rid, root_id, key, valid_from, sender, journey_id):
    return {"id": rid, "type": "initial_leg", "root_id": root_id,
            "initial_key": key, "valid_from": valid_from,
            "sender": sender, "journey_id": journey_id}


def rel_rename(rid, old, new, eff):
    return {"id": rid, "type": "rename", "old_key": old,
            "new_key": new, "effective_at": eff}


def rel_codeshare(rid, mk, op, vf, vt=None):
    d = {"id": rid, "type": "codeshare", "marketing_key": mk,
         "operating_key": op, "valid_from": vf}
    if vt:
        d["valid_to"] = vt
    return d


def write_batch(base: str, batch_no: int, sender: str,
                files: dict[str, tuple[str, str]]) -> None:
    """files: rel_path -> (content, kind)"""
    broot = os.path.join(base, f"batch_{batch_no}")
    os.makedirs(broot, exist_ok=True)
    rels = []
    kinds = {}
    for rel, (content, kind) in sorted(files.items()):
        full = os.path.join(broot, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as fh:
            fh.write(content)
        rels.append(rel)
        kinds[rel] = kind
    manifest = build_manifest(broot, rels, sender, batch_no, kinds)
    write_manifest(manifest, os.path.join(base, f"manifest_{batch_no}.json"))


def build_main(base: str) -> None:
    t08, t09, t0930, t10, t1030, t11, t12, t13, t14, t15 = (
        "2026-11-20T08:00:00+08:00", "2026-11-20T09:00:00+08:00",
        "2026-11-20T09:30:00+08:00", "2026-11-20T10:00:00+08:00",
        "2026-11-20T10:30:00+08:00", "2026-11-20T11:00:00+08:00",
        "2026-11-20T12:00:00+08:00", "2026-11-20T13:00:00+08:00",
        "2026-11-20T14:00:00+08:00", "2026-11-20T15:00:00+08:00")

    id1 = identity_file([
        rel_initial("IL-L", "L", L_OLD, t08, AUTH, "J1"),
        rel_initial("IL-M", "M", M_KEY, t08, AUTH, "J1"),
        rel_initial("IL-N", "N", N_KEY, t08, AUTH, "J2"),
        rel_rename("RN-L-10", L_OLD, L_NEW, t10),
        rel_codeshare("CS-L", MK_L, L_OLD, t08),
        rel_codeshare("CS-N", MK_N, N_KEY, t08),
    ])

    b1 = {
        "identity_b1.json": (id1, "identity_json"),
        # 08:30 scheduled + dep gate (F key, rev 1)
        "msg_E-L-01.xml": (aidx_xml(
            event_id="E-L-01", sender=AUTH, revision=1,
            effective_at="2026-11-20T08:30:00+08:00", identity_at=t08,
            times={"sched_dep": "2026-11-20T13:00:00+08:00",
                   "sched_arr": "2026-11-20T15:20:00+08:00",
                   "est_dep": "2026-11-20T13:00:00+08:00"},
            gates={"dep_gate": "T3-C12", "arr_gate": "T2-A07"}), "aidx_xml"),
        # M scheduled
        "msg_E-M-01.xml": (aidx_xml(
            event_id="E-M-01", sender=AUTH, revision=1,
            effective_at="2026-11-20T08:45:00+08:00", identity_at=t08,
            flight_no="300", dep="SHA", arr="CAN",
            times={"sched_dep": "2026-11-20T16:00:00+08:00",
                   "sched_arr": "2026-11-20T18:10:00+08:00"},
            gates={"dep_gate": "T2-B03"}), "aidx_xml"),
        # N scheduled (other operating day, same flight number)
        "msg_E-N-01.xml": (aidx_xml(
            event_id="E-N-01", sender=AUTH, revision=1,
            effective_at="2026-11-20T08:50:00+08:00", identity_at=t08,
            service_date="2026-11-21",
            times={"sched_dep": "2026-11-21T13:00:00+08:00",
                   "sched_arr": "2026-11-21T15:20:00+08:00"}), "aidx_xml"),
        # non-authoritative external evidence about L (must not project)
        "msg_E-L-EXT.xml": (aidx_xml(
            event_id="E-L-EXT", sender=OTHER, revision=9,
            effective_at="2026-11-20T09:15:00+08:00", identity_at=t09,
            times={"est_dep": "2026-11-20T19:00:00+08:00"}), "aidx_xml"),
    }
    write_batch(os.path.join(base, "main"), 1, AUTH, b1)

    # Batch 2: rename arrived at 10:00 (relation already in b1 here; keep
    # identity file cumulative-free — relations are snapshot-accumulated by
    # the warehouse itself, so later batches only carry NEW relations).
    b2 = {
        # 11:00 receipt; message written against OLD key F with identity_at 09:00
        # effective 09:30 -> late correction visible at 09:45 only AFTER b2
        "msg_E-L-02.xml": (aidx_xml(
            event_id="E-L-02", sender=AUTH, revision=2,
            effective_at=t0930, identity_at=t09,
            times={"est_dep": t12}), "aidx_xml"),
        # identical retransmission of E-L-01 (folds, no conflict)
        "msg_E-L-01-RE.xml": (aidx_xml(
            event_id="E-L-01", sender=AUTH, revision=1,
            effective_at="2026-11-20T08:30:00+08:00", identity_at=t08,
            times={"sched_dep": "2026-11-20T13:00:00+08:00",
                   "sched_arr": "2026-11-20T15:20:00+08:00",
                   "est_dep": "2026-11-20T13:00:00+08:00"},
            gates={"dep_gate": "T3-C12", "arr_gate": "T2-A07"}), "aidx_xml"),
    }
    write_batch(os.path.join(base, "main"), 2, AUTH, b2)

    # Batch 3: sparse gate clear (est/actual retained), actual time,
    # journey cancellation of J1 (L and M, not N)
    b3 = {
        "msg_E-L-03.xml": (aidx_xml(
            event_id="E-L-03", sender=AUTH, revision=3,
            effective_at=t13, identity_at=t11, carrier="CA", flight_no="200",
            gates={"dep_gate": None},
            times={"act_dep": "2026-11-20T13:07:00+08:00"}), "aidx_xml"),
        "msg_E-L-CAN.xml": (aidx_xml(
            event_id="E-L-CAN", sender=AUTH, revision=1, mtype="Cancellation",
            effective_at=t14, identity_at=t13, carrier="CA", flight_no="200",
            cancellation={"scope": "journey", "action": "cancel",
                          "journey_id": "J1"}), "aidx_xml"),
    }
    write_batch(os.path.join(base, "main"), 3, AUTH, b3)


def build_edge(base: str) -> None:
    t = "2026-11-22T08:00:00+08:00"
    t1 = "2026-11-22T09:00:00+08:00"
    t2 = "2026-11-22T10:00:00+08:00"
    A = dict(carrier="CA", flight_no="401", suffix="",
             service_date="2026-11-22", departure="PEK", arrival="CTU")
    B = dict(carrier="CA", flight_no="402", suffix="",
             service_date="2026-11-22", departure="PEK", arrival="CTU")
    C = dict(A, flight_no="403")
    D = dict(A, flight_no="404")
    E = dict(A, flight_no="405")

    # Batch 101: double-root rename + unknown-root codeshare + cycle + fork
    id_bad = identity_file([
        rel_initial("IL-A", "A", A, t, AUTH, "JA"),
        rel_initial("IL-B", "B", B, t, AUTH, "JB"),
        # rename would fuse two declared roots
        rel_rename("RN-DOUBLE", A, B, t1),
        # rename into nowhere
        rel_rename("RN-UNKNOWN",
                   dict(A, flight_no="999"), dict(A, flight_no="998"), t1),
        # rename cycle 403 -> 404 -> 403
        rel_rename("RN-C1", C, D, t1),
        rel_rename("RN-C2", D, C, t2),
        # fork: 405 renamed to two successors
        rel_rename("RN-F1", E, dict(A, flight_no="406"), t1),
        rel_rename("RN-F2", E, dict(A, flight_no="407"), t1),
        # codeshare to unknown operating root
        rel_codeshare("CS-BAD",
                      dict(carrier="KA", flight_no="2001", suffix="",
                           service_date="2026-11-22", departure="PEK",
                           arrival="CTU"),
                      dict(A, flight_no="888"), t),
    ])
    write_batch(os.path.join(base, "edge"), 1, AUTH,
                {"identity_edge.json": (id_bad, "identity_json")})

    # Batch 102: codeshare overlap onto two distinct roots, plus a
    # same-instant field conflict and a bad-offset / unsupported payload.
    # Root G is clean (no problematic rename) so field-level conflict and the
    # "conflict must not spread" rule are observable independently of A.
    G = dict(carrier="CA", flight_no="410", suffix="",
             service_date="2026-11-22", departure="PEK", arrival="CTU")
    MKX = dict(carrier="KA", flight_no="3001", suffix="",
               service_date="2026-11-22", departure="PEK", arrival="CTU")
    id_ov = identity_file([
        rel_initial("IL-G", "G", G, t, AUTH, "JG"),
        rel_codeshare("CS-OV1", MKX, A, t),
        rel_codeshare("CS-OV2", MKX, B, t),
    ])
    g_base = aidx_xml(
        event_id="E-G-BASE", sender=AUTH, revision=1, effective_at=t,
        identity_at=t, flight_no="410", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"sched_dep": "2026-11-22T10:00:00+08:00"})
    g_c1 = aidx_xml(
        event_id="E-G-C1", sender=AUTH, revision=5, effective_at=t1,
        identity_at=t, flight_no="410", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T11:00:00+08:00"})
    g_c2 = aidx_xml(
        event_id="E-G-C2", sender=AUTH, revision=5, effective_at=t1,
        identity_at=t, flight_no="410", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T11:30:00+08:00"})
    conflict_a = aidx_xml(
        event_id="E-A-C1", sender=AUTH, revision=5, effective_at=t1,
        identity_at=t, flight_no="401", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T11:00:00+08:00"})
    conflict_b = aidx_xml(
        event_id="E-A-C2", sender=AUTH, revision=5, effective_at=t1,
        identity_at=t, flight_no="401", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T11:30:00+08:00"})
    bad_tz = aidx_xml(
        event_id="E-A-BADTZ", sender=AUTH, revision=1, effective_at=t1,
        identity_at=t, flight_no="401", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T11:00:00+08:00"}, bad_offset=True)
    unsupported = aidx_xml(
        event_id="E-A-UNSUP", sender=AUTH, revision=1, effective_at=t1,
        identity_at=t, flight_no="401", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T11:00:00+08:00"},
        unsupported="PNR12345")
    write_batch(os.path.join(base, "edge"), 2, AUTH, {
        "identity_overlap.json": (id_ov, "identity_json"),
        "msg_g_base.xml": (g_base, "aidx_xml"),
        "msg_g_conflict_1.xml": (g_c1, "aidx_xml"),
        "msg_g_conflict_2.xml": (g_c2, "aidx_xml"),
        "msg_conflict_1.xml": (conflict_a, "aidx_xml"),
        "msg_conflict_2.xml": (conflict_b, "aidx_xml"),
        "msg_bad_offset.xml": (bad_tz, "aidx_xml"),
        "msg_unsupported.xml": (unsupported, "aidx_xml"),
    })

    # Batch 103: forward reference — message to a key whose root arrives later
    fwd_msg = aidx_xml(
        event_id="E-FWD-1", sender=AUTH, revision=1, effective_at=t1,
        identity_at=t, flight_no="500", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"sched_dep": "2026-11-22T12:00:00+08:00"})
    write_batch(os.path.join(base, "edge"), 3, AUTH,
                {"msg_forward.xml": (fwd_msg, "aidx_xml")})

    # Batch 104: root for 500 now declared -> forward message binds at publish
    FKEY = dict(carrier="CA", flight_no="500", suffix="",
                service_date="2026-11-22", departure="PEK", arrival="CTU")
    id_fwd = identity_file([rel_initial("IL-F", "F", FKEY, t, AUTH, "JF")])
    write_batch(os.path.join(base, "edge"), 4, AUTH,
                {"identity_forward.json": (id_fwd, "identity_json")})

    # Batch 105: unknown/non-authoritative sender targeting the CLEAN root G
    # (flight 410). It must be retained as external evidence and never project
    # its 15:00 estimate over G's authoritative conflict state.
    unknown = aidx_xml(
        event_id="E-UNK-1", sender=UNKNOWN, revision=1, effective_at=t1,
        identity_at=t, flight_no="410", service_date="2026-11-22",
        dep="PEK", arr="CTU",
        times={"est_dep": "2026-11-22T15:00:00+08:00"})
    write_batch(os.path.join(base, "edge"), 5, AUTH,
                {"msg_unknown_sender.xml": (unknown, "aidx_xml")})

    # Batch 106: cross-UTC-midnight times: service date stays 2026-11-22 even
    # though an actual time expressed in UTC falls on 2026-11-23.
    XKEY = dict(carrier="CA", flight_no="600", suffix="",
                service_date="2026-11-22", departure="URC", arrival="PEK")
    id_x = identity_file([rel_initial("IL-X", "X", XKEY, t, AUTH, "JX")])
    cross = aidx_xml(
        event_id="E-X-1", sender=AUTH, revision=1,
        effective_at="2026-11-22T23:30:00+08:00",
        identity_at="2026-11-22T08:00:00+08:00",
        flight_no="600", service_date="2026-11-22", dep="URC", arr="PEK",
        times={"act_dep": "2026-11-23T00:15:00+08:00",
               "act_arr": "2026-11-22T16:30:00Z"})
    write_batch(os.path.join(base, "edge"), 6, AUTH, {
        "identity_crossday.json": (id_x, "identity_json"),
        "msg_crossday.xml": (cross, "aidx_xml"),
    })
    labels = {
        "1": "logical 101: double-root rename, unknown-root codeshare, cycle, fork",
        "2": "logical 102: codeshare overlap, same-instant field conflict, bad offset, unsupported element",
        "3": "logical 103: forward reference before root declaration",
        "4": "logical 104: forward root arrives and late message binds",
        "5": "logical 105: unknown sender external evidence",
        "6": "logical 106: cross-UTC-midnight times keep local service date",
    }
    with open(os.path.join(base, "edge", "SUITE_LABELS.json"), "w",
              encoding="utf-8") as fh:
        json.dump(labels, fh, ensure_ascii=False, indent=2)


def build_large(base: str) -> None:
    """One batch with many large messages; blobs live on disk, not in RAM."""
    t = "2026-11-23T08:00:00+08:00"
    rels = []
    files = {}
    n_legs = 400
    for i in range(n_legs):
        fno = 7000 + i
        key = dict(carrier="CA", flight_no=str(fno), suffix="",
                   service_date="2026-11-23", departure="PEK", arrival="SHA")
        rels.append(rel_initial(f"IL-{fno}", f"R{fno}", key, t, AUTH, f"J{fno}"))
        # ~20 KB of ignorable whitespace/comments to make big bodies
        padding = "\n     <!-- pad " + ("x" * 512) + " -->\n"
        xml = aidx_xml(
            event_id=f"E-BIG-{fno}", sender=AUTH, revision=1,
            effective_at=t, identity_at=t, flight_no=str(fno),
            service_date="2026-11-23",
            times={"sched_dep": "2026-11-23T13:00:00+08:00",
                   "est_dep": "2026-11-23T13:05:00+08:00"},
            gates={"dep_gate": f"C{i % 40}"})
        xml = xml.replace("</FlightLeg>", padding * 40 + "   </FlightLeg>")
        files[f"msg_{fno}.xml"] = (xml, "aidx_xml")
    files["identity_large.json"] = (identity_file(rels), "identity_json")
    write_batch(os.path.join(base, "large"), 1, AUTH, files)


# Hand-written expectations (independent of the projection implementation).
# Every state/value below was derived by manually replaying the fixture
# messages; the checker compares engine output against these literals and
# never imports projection code to produce them.
WALKTHROUGH_EXPECTED = {
    "description": "Manually specified expectations for the main walkthrough. "
                   "Computed by hand from the fixtures, never generated by the "
                   "query engine. Checker compares engine output against these.",
    "root_L": "CA100 2026-11-20 PEK->SHA (renamed CA200 at 10:00 local)",
    "keys": {
        "OP":     {"carrier": "CA", "flight_no": "100", "suffix": "",
                   "service_date": "2026-11-20", "departure": "PEK",
                   "arrival": "SHA", "kind": "OP"},
        "OP-NEW": {"carrier": "CA", "flight_no": "200", "suffix": "",
                   "service_date": "2026-11-20", "departure": "PEK",
                   "arrival": "SHA", "kind": "OP"},
        "MK":     {"carrier": "KA", "flight_no": "1000", "suffix": "",
                   "service_date": "2026-11-20", "departure": "PEK",
                   "arrival": "SHA", "kind": "MK"},
        "N":      {"carrier": "CA", "flight_no": "100", "suffix": "",
                   "service_date": "2026-11-21", "departure": "PEK",
                   "arrival": "SHA", "kind": "OP"},
        "MK-N":   {"carrier": "KA", "flight_no": "1000", "suffix": "",
                   "service_date": "2026-11-21", "departure": "PEK",
                   "arrival": "SHA", "kind": "MK"},
        "M":      {"carrier": "CA", "flight_no": "300", "suffix": "",
                   "service_date": "2026-11-20", "departure": "SHA",
                   "arrival": "CAN", "kind": "OP"},
    },
    "cases": [
        {
            "case": "b1 @09:45 operating old key F=CA100",
            "receipt_batch": 1,
            "business_time": "2026-11-20T09:45:00+08:00",
            "key": "OP",
            "expected_root": "L",
            "fields": {
                "sched_dep": {"state": "value", "value": "2026-11-20T13:00:00+08:00"},
                "est_dep": {"state": "value", "value": "2026-11-20T13:00:00+08:00"},
                "act_dep": {"state": "absent"},
                "dep_gate": {"state": "value", "value": "T3-C12"},
            },
            "cancellation": "absent",
            "note": "E-L-02 (est 12:00, effective 09:30) only arrives in batch 2"
        },
        {
            "case": "b2 @09:45 late correction now visible, b1 stays frozen",
            "receipt_batch": 2,
            "business_time": "2026-11-20T09:45:00+08:00",
            "key": "OP",
            "expected_root": "L",
            "fields": {
                "est_dep": {"state": "value", "value": "2026-11-20T12:00:00+08:00",
                            "winning_event": "E-L-02"},
            },
            "cancellation": "absent",
            "note": "late message used retired F key with identity_at=09:00; "
                    "batch 1 knowledge is not rewritten by this later message"
        },
        {
            "case": "b1 @10:30 marketing KA1000 resolves to the same L",
            "receipt_batch": 1,
            "business_time": "2026-11-20T10:30:00+08:00",
            "key": "MK",
            "expected_root": "L",
            "fields": {"sched_dep": {"state": "value"}},
            "cancellation": "absent"
        },
        {
            "case": "b2 @10:30 new operating key CA200 resolves to L after rename",
            "receipt_batch": 2,
            "business_time": "2026-11-20T10:30:00+08:00",
            "key": "OP-NEW",
            "expected_root": "L",
            "fields": {"sched_dep": {"state": "value"}},
            "cancellation": "absent"
        },
        {
            "case": "b3 @13:30 sparse gate clear; est and actual retained",
            "receipt_batch": 3,
            "business_time": "2026-11-20T13:30:00+08:00",
            "key": "OP-NEW",
            "expected_root": "L",
            "fields": {
                "dep_gate": {"state": "cleared"},
                "est_dep": {"state": "value", "value": "2026-11-20T12:00:00+08:00"},
                "act_dep": {"state": "value", "value": "2026-11-20T13:07:00+08:00"},
            },
            "cancellation": "absent",
            "note": "E-L-03 (effective 13:00) clears only dep_gate and adds "
                    "act_dep; est_dep from E-L-02 survives"
        },
        {
            "case": "b3 @14:30 journey J1 cancelled: L cancelled, times kept",
            "receipt_batch": 3,
            "business_time": "2026-11-20T14:30:00+08:00",
            "key": "OP-NEW",
            "expected_root": "L",
            "fields": {
                "est_dep": {"state": "value"},
                "act_dep": {"state": "value", "value": "2026-11-20T13:07:00+08:00"},
            },
            "cancellation": "cancelled"
        },
        {
            "case": "journey J1 cancellation also hits companion leg M",
            "receipt_batch": 3,
            "business_time": "2026-11-20T14:30:00+08:00",
            "key": "M",
            "expected_root": "M",
            "fields": {"sched_dep": {"state": "value"}},
            "cancellation": "cancelled"
        },
        {
            "case": "N (other operating day, same flight number) never cancelled",
            "receipt_batch": 3,
            "business_time": "2026-11-20T14:30:00+08:00",
            "key": "N",
            "expected_root": "N",
            "fields": {"sched_dep": {"state": "value",
                                     "value": "2026-11-21T13:00:00+08:00"}},
            "cancellation": "absent",
            "note": "N is journey J2; journey J1 cancellation must not bleed "
                    "across operating days or same flight numbers"
        },
        {
            "case": "marketing KA1000 on 2026-11-21 binds N not L",
            "receipt_batch": 3,
            "business_time": "2026-11-21T09:00:00+08:00",
            "key": "MK-N",
            "expected_root": "N",
            "cancellation": "absent"
        }
    ]
}


def main(out: str | None = None) -> None:
    base = out or os.path.join(HERE, "..", "fixtures")
    base = os.path.abspath(base)
    os.makedirs(base, exist_ok=True)
    build_main(base)
    build_edge(base)
    build_large(base)
    exp_dir = os.path.join(base, "expected")
    os.makedirs(exp_dir, exist_ok=True)
    with open(os.path.join(exp_dir, "walkthrough_expected.json"), "w",
              encoding="utf-8") as fh:
        json.dump(WALKTHROUGH_EXPECTED, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"fixture_root": base,
                      "batches": ["main:1-3", "edge:101-106", "large:201"]},
                     indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
