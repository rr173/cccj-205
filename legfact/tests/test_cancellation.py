#!/usr/bin/env python3
"""Focused cancellation semantics tests.

Verifies, with purpose-built messages in an isolated warehouse:
  * leg vs journey scope;
  * same-priority cancel/restore => conflict (no side picked);
  * restore is an explicit operation, never inferred;
  * cancellation never erases scheduled/actual times;
  * journey cancellation follows identity membership, not flight numbers.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from generate_fixtures import (aidx_xml, identity_file, rel_initial,  # noqa: E402
                               rel_rename, write_batch, AUTH)
from legfact import OpKey, QueryEngine, Warehouse  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, cond):
    RESULTS.append((name, cond))
    print(("ok   " if cond else "FAIL ") + name)


def main() -> int:
    d = tempfile.mkdtemp(prefix="wh_cancel_")
    t0 = "2026-11-25T08:00:00+08:00"
    K = dict(carrier="CA", flight_no="900", suffix="",
             service_date="2026-11-25", departure="PEK", arrival="SZX")
    K2 = dict(carrier="CA", flight_no="901", suffix="",
              service_date="2026-11-25", departure="PEK", arrival="SZX")
    idoc = identity_file([
        rel_initial("IL-K", "K", K, t0, AUTH, "JK"),
        rel_initial("IL-K2", "K2", K2, t0, AUTH, "JL"),
    ])
    write_batch(os.path.join(d, "in"), 1, AUTH,
                {"id.json": (idoc, "identity_json"),
                 "base.xml": (aidx_xml(
                     event_id="E-BASE", sender=AUTH, revision=1,
                     effective_at="2026-11-25T08:30:00+08:00", identity_at=t0,
                     flight_no="900", service_date="2026-11-25",
                     dep="PEK", arr="SZX",
                     times={"sched_dep": "2026-11-25T13:00:00+08:00",
                            "act_dep": "2026-11-25T13:05:00+08:00"}),
                     "aidx_xml")})
    # leg cancel then same-instant restore (different events, equal priority)
    write_batch(os.path.join(d, "in"), 2, AUTH, {
        "cancel.xml": (aidx_xml(
            event_id="E-CAN", sender=AUTH, revision=2, mtype="Cancellation",
            effective_at="2026-11-25T10:00:00+08:00", identity_at=t0,
            flight_no="900", service_date="2026-11-25", dep="PEK", arr="SZX",
            cancellation={"scope": "leg", "action": "cancel"}), "aidx_xml"),
        "restore.xml": (aidx_xml(
            event_id="E-RES", sender=AUTH, revision=2, mtype="Cancellation",
            effective_at="2026-11-25T10:00:00+08:00", identity_at=t0,
            flight_no="900", service_date="2026-11-25", dep="PEK", arr="SZX",
            cancellation={"scope": "leg", "action": "restore"}), "aidx_xml"),
    })
    # later explicit restore at higher effective_at resolves the conflict
    write_batch(os.path.join(d, "in"), 3, AUTH, {
        "restore2.xml": (aidx_xml(
            event_id="E-RES2", sender=AUTH, revision=3, mtype="Cancellation",
            effective_at="2026-11-25T11:00:00+08:00", identity_at=t0,
            flight_no="900", service_date="2026-11-25", dep="PEK", arr="SZX",
            cancellation={"scope": "leg", "action": "restore"}), "aidx_xml"),
    })
    # journey JL cancel must not touch K (different journey), must touch K2
    write_batch(os.path.join(d, "in"), 4, AUTH, {
        "jcancel.xml": (aidx_xml(
            event_id="E-JCAN", sender=AUTH, revision=1, mtype="Cancellation",
            effective_at="2026-11-25T12:00:00+08:00", identity_at=t0,
            flight_no="901", service_date="2026-11-25", dep="PEK", arr="SZX",
            cancellation={"scope": "journey", "action": "cancel",
                          "journey_id": "JL"}), "aidx_xml"),
    })

    wh = Warehouse(os.path.join(d, "wh"))
    for b in (1, 2, 3, 4):
        wh.ingest_batch(os.path.join(d, "in", f"manifest_{b}.json"),
                        os.path.join(d, "in", f"batch_{b}"))
    eng = QueryEngine(wh)
    Kkey = OpKey("CA", "900", "", "2026-11-25", "PEK", "SZX", "OP")
    K2key = OpKey("CA", "901", "", "2026-11-25", "PEK", "SZX", "OP")

    r2 = QueryEngine.to_dict(eng.query(Kkey, 2, "2026-11-25T10:30:00+08:00"))
    check("cancel/restore equal priority -> conflict",
          r2["cancellation"]["state"] == "conflict"
          and {a["action"] for a in r2["cancellation"]["actions"]}
              == {"cancel", "restore"})
    check("conflict does not erase actual/scheduled times",
          r2["fields"]["act_dep"]["state"] == "value"
          and r2["fields"]["sched_dep"]["state"] == "value")

    r3 = QueryEngine.to_dict(eng.query(Kkey, 3, "2026-11-25T11:30:00+08:00"))
    check("later explicit restore wins (never inferred)",
          r3["cancellation"]["state"] == "restored"
          and r3["cancellation"]["actions"][0]["event_id"] == "E-RES2")

    r4k = QueryEngine.to_dict(eng.query(Kkey, 4, "2026-11-25T12:30:00+08:00"))
    r4k2 = QueryEngine.to_dict(eng.query(K2key, 4, "2026-11-25T12:30:00+08:00"))
    check("journey JL cancel does not touch K (journey JK)",
          r4k["cancellation"]["state"] == "restored")
    check("journey JL cancel touches member K2 by identity, not flight number",
          r4k2["cancellation"]["state"] == "cancelled")
    check("K2 still carries its (absent here) times independently; K times kept",
          r4k["fields"]["act_dep"]["value"] == "2026-11-25T13:05:00+08:00")
    wh.close()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS)-len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
