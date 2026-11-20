#!/usr/bin/env python3
"""Independent checker: engine output vs the hand-written expectation tables.

The expectations live in fixtures/expected/*.json and were derived by hand,
not by the projection code.  This module only consumes the public
Warehouse/QueryEngine API and compares its output against those literals, so
it is an independent oracle rather than a self-consistent assertion.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact import OpKey, QueryEngine, Warehouse  # noqa: E402

FIX = os.path.abspath(os.path.join(HERE, "..", "fixtures"))


def _load_expected() -> dict:
    with open(os.path.join(FIX, "expected", "walkthrough_expected.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


def _key(spec: dict) -> OpKey:
    return OpKey(spec["carrier"], spec["flight_no"], spec["suffix"],
                 spec["service_date"], spec["departure"], spec["arrival"],
                 spec["kind"])


def run_walkthrough(wh_path: str) -> tuple[int, int, list[str]]:
    expected = _load_expected()
    wh = Warehouse(wh_path)
    eng = QueryEngine(wh)
    passed = failed = 0
    errors: list[str] = []

    def check(cond: bool, label: str) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            errors.append(label)

    for case in expected["cases"]:
        spec = expected["keys"][case["key"]]
        res = QueryEngine.to_dict(eng.query(
            _key(spec), case["receipt_batch"], case["business_time"]))
        tag = f"[{case['case']}]"
        check(res["binding"]["status"] == "bound",
              f"{tag} binding expected bound, got {res['binding']['status']}")
        check(res["binding"]["root_id"] == case["expected_root"],
              f"{tag} root expected {case['expected_root']}, "
              f"got {res['binding']['root_id']}")
        for fname, want in case.get("fields", {}).items():
            got = res["fields"][fname]
            check(got["state"] == want["state"],
                  f"{tag} {fname} state expected {want['state']}, got {got['state']}")
            if "value" in want:
                check(got.get("value") == want["value"],
                      f"{tag} {fname} value expected {want['value']}, got {got.get('value')}")
            if "winning_event" in want:
                win = got.get("winning_source") or {}
                check(win.get("event_id") == want["winning_event"],
                      f"{tag} {fname} winning event expected "
                      f"{want['winning_event']}, got {win.get('event_id')}")
        check(res["cancellation"]["state"] == case["cancellation"],
              f"{tag} cancellation expected {case['cancellation']}, "
              f"got {res['cancellation']['state']}")

    # extra provenance assertions not expressible in the compact table
    old = expected["keys"]["OP"]
    b1 = QueryEngine.to_dict(eng.query(_key(old), 1, "2026-11-20T09:45:00+08:00"))
    b2 = QueryEngine.to_dict(eng.query(_key(old), 2, "2026-11-20T09:45:00+08:00"))
    check(b1["fields"]["est_dep"]["value"] == "2026-11-20T13:00:00+08:00",
          "[immutability] batch1 keeps pre-correction estimate")
    check(b2["fields"]["est_dep"]["value"] == "2026-11-20T12:00:00+08:00",
          "[immutability] batch2 sees late correction")
    win = b2["fields"]["est_dep"]["winning_source"]
    check(win is not None and win["receipt_batch"] == 2
          and win["raw_file"].endswith(".xml") and win["xml_location"],
          "[provenance] winning source cites batch, file and XML location")
    # marketing result must state which operating root it returned
    mk = QueryEngine.to_dict(eng.query(_key(expected["keys"]["MK"]), 1,
                                       "2026-11-20T10:30:00+08:00"))
    check(mk["queried_key_kind"] == "marketing"
          and mk["binding"]["root_id"] == "L"
          and any(h["via"] == "codeshare" for h in mk["binding"]["identity_chain"]),
          "[marketing] codeshare query names operating root L and shows chain")
    wh.close()
    return passed, failed, errors


if __name__ == "__main__":
    wh_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/wh_walkthrough"
    p, f, errs = run_walkthrough(wh_path)
    for e in errs:
        print("FAIL", e)
    print(json.dumps({"passed": p, "failed": f}, indent=2))
    sys.exit(1 if f else 0)
