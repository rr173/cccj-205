#!/usr/bin/env python3
"""Independent per-checkpoint oracle for the arrival backtest.

This checker never imports the backtest engine.  It rebuilds every checkpoint
fact directly from the legfact warehouse tables and the frozen inputs:

* it loads the *historical* identity graph of each visible batch itself and
  re-binds every message row at the row's own ``identity_at``;
* it replays field rows with a deliberately naive per-root selection
  (max effective_at, then max source_revision, distinct payload => conflict)
  written independently of the streaming bucket store;
* it classifies every sample with the precedence rules spelled out in the
  brief, recomputes every error in integer microseconds, and then compares its
  own rows and denominators one-by-one against the engine's ``samples.ndjson``
  and ``summary.json``.

Run:  python3 -I tests/check_backtest.py --out-dir DIR --warehouse W \
          --spec S --roster R --visibility V
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact import Warehouse  # noqa: E402
from legfact import identity as ident_mod  # noqa: E402
from legfact.timeutil import parse_offset_datetime  # noqa: E402

MIN_US = 60 * 1_000_000
CLASSES = ("unobservable", "known_landed_at_checkpoint",
           "close_out_undetermined", "later_known_already_landed",
           "target_population")
FIELDS = ("sched_arr", "est_arr", "act_arr")


def to_us(text: str) -> int:
    return _us(parse_offset_datetime(text).utc)


def _us(dt) -> int:
    from datetime import timezone
    e = dt.astimezone(timezone.utc)
    return int(e.timestamp()) * 1_000_000 + e.microsecond


def load_ndjson(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def visible_batch(vis_rows: list[dict], instant_us: int) -> int:
    best = 0
    for v in vis_rows:
        if to_us(v["first_visible_at"]) <= instant_us:
            best = max(best, v["batch_no"])
    return best


def naive_field(wh: Warehouse, graph, root_id: str, airport: str,
                batch: int, field: str, cutoff_us: int):
    """Independently adjudicate one field at one (batch, instant) cutoff."""
    conn = wh._conn
    rows = conn.execute(
        "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
        "e.effective_at, e.identity_at, e.raw_ref, "
        "k.carrier, k.flight_no, k.suffix, k.service_date, "
        "k.departure, k.arrival, f.state, f.value, f.xml_location, "
        "f.line, f.columnn "
        "FROM event_fields f "
        "JOIN events e ON e.event_id=f.event_id AND e.batch_id=f.batch_id "
        "JOIN event_keys k ON k.event_id=e.event_id AND k.batch_id=e.batch_id "
        "WHERE f.field=? AND e.batch_id<=? "
        "ORDER BY e.event_id, e.batch_id",
        (field, batch)).fetchall()
    chosen: list[dict] = []
    for r in rows:
        eff_us = to_us(r["effective_at"])
        if eff_us > cutoff_us:
            continue
        key = ident_mod.OpKey(
            r["carrier"], r["flight_no"], r["suffix"], r["service_date"],
            r["departure"], r["arrival"], "OP")
        b = graph.bind_operating(key, parse_offset_datetime(r["identity_at"]))
        if b.status != "bound" or b.root_id != root_id \
                or b.sender != r["sender"]:
            continue
        chosen.append({"eff_us": eff_us, "eff": r["effective_at"],
                       "rev": r["source_revision"], "state": r["state"],
                       "value": r["value"], "event_id": r["event_id"],
                       "batch_id": r["batch_id"]})
    if not chosen:
        return {"state": "absent", "value": None, "evidence": []}
    max_eff = max(c["eff_us"] for c in chosen)
    top_eff = [c for c in chosen if c["eff_us"] == max_eff]
    max_rev = max(c["rev"] for c in top_eff)
    top = [c for c in top_eff if c["rev"] == max_rev]
    distinct = {(c["state"], c["value"]) for c in top}
    if len(distinct) > 1:
        return {"state": "conflict", "value": None,
                "evidence": [{"event_id": c["event_id"],
                              "receipt_batch": c["batch_id"]} for c in top]}
    win = top[0]
    return {"state": "cleared" if win["state"] == "cleared" else "value",
            "value": win["value"],
            "winning_effective_at": win["eff"],
            "evidence": [{"event_id": win["event_id"],
                          "receipt_batch": win["batch_id"]}]}


def root_state(graph, root_id: str, airport: str) -> str:
    il = graph.initials.get(root_id)
    if il is None:
        return "unavailable"
    if il.key.arrival != airport:
        return "airport_mismatch"
    return "available"


def p90(values: list[int]) -> int:
    return sorted(values)[math.ceil(0.9 * len(values)) - 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--roster", required=True)
    ap.add_argument("--visibility", required=True)
    args = ap.parse_args()

    spec = json.load(open(args.spec, encoding="utf-8"))
    roster = load_ndjson(args.roster)
    vis = sorted(load_ndjson(args.visibility),
                 key=lambda v: v["batch_no"])
    close_us = to_us(spec["close_out_at"])
    airport = spec["airport"]

    wh = Warehouse(args.warehouse)
    try:
        close_batch = visible_batch(vis, close_us)
        close_graph = wh.load_graph(close_batch)
        graph_cache: dict[int, object] = {close_batch: close_graph}

        def graph_of(b: int):
            if b not in graph_cache:
                graph_cache[b] = wh.load_graph(b)
            return graph_cache[b]

        oracle_rows: list[dict] = []
        for rr in roster:
            root = rr["root_id"]
            sched_us = to_us(rr["roster_sched_arr"])
            for lead in spec["lead_minutes"]:
                cp_us = sched_us - lead * MIN_US
                vb = visible_batch(vis, cp_us)
                # ---- availability / binding at the checkpoint cut
                if vb == 0:
                    cp_bind = "no_batch"
                else:
                    cp_bind = root_state(graph_of(vb), root, airport)
                close_bind = root_state(close_graph, root, airport)

                # ---- independently replayed facts
                if vb == 0:
                    sched = est = cp_act = {"state": "absent",
                                            "value": None, "evidence": []}
                else:
                    g = graph_of(vb)
                    sched = naive_field(wh, g, root, airport, vb,
                                        "sched_arr", cp_us)
                    est = naive_field(wh, g, root, airport, vb,
                                      "est_arr", cp_us)
                    cp_act = naive_field(wh, g, root, airport, vb,
                                         "act_arr", cp_us)
                    # independently layer the backtest's stale rule onto the
                    # two PREDICTION fields only: a definite value whose
                    # winning effective instant is at or before the checkpoint
                    # was not refreshed after that instant.
                    for view in (sched, est):
                        if view["state"] == "value" and \
                                to_us(view["winning_effective_at"]) <= cp_us:
                            view["state"] = "stale"
                close_act = naive_field_no_cut(
                    wh, close_graph, root, airport, close_batch, "act_arr")

                # ---- classification in the exact precedence order
                cls = uno = clo = None
                if vb == 0:
                    cls, uno = "unobservable", "no_batch_visible_yet"
                elif cp_bind == "unavailable":
                    cls, uno = "unobservable", \
                        "operating_root_unavailable_at_cutoff"
                elif cp_bind == "airport_mismatch":
                    cls, uno = "unobservable", \
                        "initial_destination_not_this_airport"
                elif cp_act["state"] == "value" and cp_act["value"] and \
                        to_us(cp_act["value"]) <= cp_us:
                    cls = "known_landed_at_checkpoint"
                elif close_bind in ("unavailable", "airport_mismatch"):
                    cls, clo = "close_out_undetermined", \
                        "no_airport_operating_root_at_close_out"
                elif close_act["state"] in ("absent", "cleared", "conflict"):
                    cls, clo = "close_out_undetermined", \
                        "actual_arrival_uncertain"
                elif to_us(close_act["value"]) > close_us:
                    cls, clo = "close_out_undetermined", \
                        "actual_arrival_after_close_out"
                elif to_us(close_act["value"]) <= cp_us:
                    cls = "later_known_already_landed"
                else:
                    cls = "target_population"

                sched_err = est_err = answer = answer_us = None
                if cls == "target_population":
                    answer = close_act["value"]
                    answer_us = to_us(answer)
                    if sched["state"] in ("value", "stale") and sched["value"]:
                        sched_err = to_us(sched["value"]) - answer_us
                    if est["state"] in ("value", "stale") and est["value"]:
                        est_err = to_us(est["value"]) - answer_us
                oracle_rows.append({
                    "root_id": root, "group": rr["group"],
                    "lead_minutes": lead, "visible_batch": vb,
                    "classification": cls, "unobservable_reason": uno,
                    "close_undetermined_reason": clo,
                    "checkpoint_binding": cp_bind,
                    "close_out_binding": close_bind,
                    "sched_state": sched["state"],
                    "est_state": est["state"],
                    "cp_act_state": cp_act["state"],
                    "close_act_state": close_act["state"],
                    "answer": answer,
                    "sched_error_us": sched_err,
                    "est_error_us": est_err,
                    "sched_ev": [e["event_id"] for e in sched["evidence"]],
                    "est_ev": [e["event_id"] for e in est["evidence"]],
                    "close_ev": [e["event_id"] for e in close_act["evidence"]],
                })
    finally:
        wh.close()

    # ---- compare against the engine's emitted detail rows
    engine_rows = [json.loads(l)
                   for l in open(os.path.join(args.out_dir, "samples.ndjson"),
                                 encoding="utf-8")]
    errors: list[str] = []
    if len(engine_rows) != len(oracle_rows):
        errors.append(
            f"row count {len(engine_rows)} != oracle {len(oracle_rows)}")
    index = {(r["root_id"], r["lead_minutes"]): r for r in engine_rows}
    for o in oracle_rows:
        e = index.get((o["root_id"], o["lead_minutes"]))
        if e is None:
            errors.append(f"missing engine row for {o['root_id']} "
                          f"lead {o['lead_minutes']}")
            continue
        pairs = [
            ("classification", o["classification"], e["classification"]),
            ("unobservable_reason", o["unobservable_reason"],
             e["unobservable_reason"]),
            ("close_undetermined_reason", o["close_undetermined_reason"],
             e["close_undetermined_reason"]),
            ("checkpoint_binding", o["checkpoint_binding"],
             e["checkpoint_binding"]),
            ("close_out_binding", o["close_out_binding"],
             e["close_out_binding"]),
            ("visible_batch", o["visible_batch"], e["visible_batch"]),
            ("sched_state", o["sched_state"], e["sched_arr"]["state"]),
            ("est_state", o["est_state"], e["est_arr"]["state"]),
            ("cp_act_state", o["cp_act_state"],
             e["checkpoint_act_arr"]["state"]),
            ("close_act_state", o["close_act_state"],
             e["close_out_act_arr"]["state"]),
            ("sched_error_us", o["sched_error_us"], e["sched_error_us"]),
            ("est_error_us", o["est_error_us"], e["est_error_us"]),
            ("answer", o["answer"], e["answer_act_arr"]),
            ("sched_evidence", o["sched_ev"],
             [x["event_id"] for x in e["sched_arr"]["evidence"]]),
            ("est_evidence", o["est_ev"],
             [x["event_id"] for x in e["est_arr"]["evidence"]]),
            ("close_evidence", o["close_ev"],
             [x["event_id"] for x in e["close_out_act_arr"]["evidence"]]),
        ]
        for name, ov, ev in pairs:
            if ov != ev:
                errors.append(
                    f"{o['root_id']} lead{o['lead_minutes']} {name}: "
                    f"oracle={ov!r} engine={ev!r}")

    # ---- independently recompute every summary denominator and metric
    summary = json.load(open(os.path.join(args.out_dir, "summary.json"),
                             encoding="utf-8"))
    groups = sorted({r["group"] for r in oracle_rows})
    for g in groups:
        for lead in spec["lead_minutes"]:
            sub = [r for r in oracle_rows
                   if r["group"] == g and r["lead_minutes"] == lead]
            _check_block(errors, sub, summary, ("group", g, lead))
    for lead in spec["lead_minutes"]:
        sub = [r for r in oracle_rows if r["lead_minutes"] == lead]
        _check_block(errors, sub, summary, ("overall", None, lead))

    # ---- worked-example arithmetic (the brief's -8 vs -13 minutes)
    g1 = next(r for r in oracle_rows
              if r["root_id"] == "G1" and r["lead_minutes"] == 30)
    g2 = next(r for r in oracle_rows
              if r["root_id"] == "G2" and r["lead_minutes"] == 30)
    assert g1["sched_error_us"] == 600_000_000
    assert g1["est_error_us"] == -120_000_000
    assert g2["sched_error_us"] == 1_200_000_000
    assert g2["est_error_us"] is None
    paired_diff = abs(g1["est_error_us"]) - abs(g1["sched_error_us"])
    assert paired_diff == -480_000_000            # -8 minutes
    naive = (abs(g1["est_error_us"]) / 1
             - (abs(g1["sched_error_us"]) + abs(g2["sched_error_us"])) / 2)
    assert naive == -780_000_000.0               # -13 minutes

    if errors:
        print("FAIL")
        for e in errors:
            print(" -", e)
        return 1
    print(f"OK: {len(oracle_rows)} independent checkpoint rows match the "
          f"engine; every grouped/overall denominator and metric reconciles; "
          f"paired diff -8 min vs naive mean diff -13 min confirmed")
    return 0


def naive_field_no_cut(wh, graph, root_id, airport, batch, field):
    """Close-out answer: replay with no effective ceiling (future actuals)."""
    conn = wh._conn
    rows = conn.execute(
        "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
        "e.effective_at, e.identity_at, k.carrier, k.flight_no, k.suffix, "
        "k.service_date, k.departure, k.arrival, f.state, f.value "
        "FROM event_fields f "
        "JOIN events e ON e.event_id=f.event_id AND e.batch_id=f.batch_id "
        "JOIN event_keys k ON k.event_id=e.event_id AND k.batch_id=e.batch_id "
        "WHERE f.field=? AND e.batch_id<=?", (field, batch)).fetchall()
    chosen = []
    for r in rows:
        key = ident_mod.OpKey(
            r["carrier"], r["flight_no"], r["suffix"], r["service_date"],
            r["departure"], r["arrival"], "OP")
        b = graph.bind_operating(key, parse_offset_datetime(r["identity_at"]))
        if b.status == "bound" and b.root_id == root_id \
                and b.sender == r["sender"]:
            chosen.append({"eff": r["effective_at"],
                           "eff_us": to_us(r["effective_at"]),
                           "rev": r["source_revision"], "state": r["state"],
                           "value": r["value"], "event_id": r["event_id"],
                           "batch_id": r["batch_id"]})
    if not chosen:
        return {"state": "absent", "value": None, "evidence": []}
    max_eff = max(c["eff_us"] for c in chosen)
    top_eff = [c for c in chosen if c["eff_us"] == max_eff]
    max_rev = max(c["rev"] for c in top_eff)
    top = [c for c in top_eff if c["rev"] == max_rev]
    if len({(c["state"], c["value"]) for c in top}) > 1:
        return {"state": "conflict", "value": None,
                "evidence": [{"event_id": c["event_id"],
                              "receipt_batch": c["batch_id"]} for c in top]}
    win = top[0]
    return {"state": "cleared" if win["state"] == "cleared" else "value",
            "value": win["value"], "winning_effective_at": win["eff"],
            "evidence": [{"event_id": win["event_id"],
                          "receipt_batch": win["batch_id"]}]}


def _check_block(errors, sub, summary, key):
    kind, g, lead = key
    if kind == "group":
        block = next(b for b in summary["per_group_lead"]
                     if b["group"] == g and b["lead_minutes"] == lead)
    else:
        block = next(b for b in summary["overall_per_lead"]
                     if b["lead_minutes"] == lead)
    target = [r for r in sub if r["classification"] == "target_population"]
    sched_err = [r["sched_error_us"] for r in target
                 if r["sched_error_us"] is not None]
    est_err = [r["est_error_us"] for r in target
               if r["est_error_us"] is not None]
    paired = [r for r in target
              if r["sched_error_us"] is not None
              and r["est_error_us"] is not None]
    expect = {
        "roster_points": len(sub),
        "target_population": len(target),
        "sched_coverage_n": len(sched_err),
        "est_coverage_n": len(est_err),
        "both_available_n": len(paired),
        "unobservable": sum(
            r["classification"] == "unobservable" for r in sub),
        "known_landed": sum(
            r["classification"] == "known_landed_at_checkpoint" for r in sub),
        "close_undetermined": sum(
            r["classification"] == "close_out_undetermined" for r in sub),
        "later_known": sum(
            r["classification"] == "later_known_already_landed" for r in sub),
    }
    got = {
        "roster_points": block["roster_points"],
        "target_population": block["target_population"],
        "sched_coverage_n": block["sched_coverage_n"],
        "est_coverage_n": block["est_coverage_n"],
        "both_available_n": block["both_available_n"],
        "unobservable": block["exclusions"]["unobservable"],
        "known_landed": block["exclusions"]["known_landed_at_checkpoint"],
        "close_undetermined":
            block["exclusions"]["close_out_undetermined"],
        "later_known": block["exclusions"]["later_known_already_landed"],
    }
    tag = f"{kind} {g} lead{lead}"
    for name in expect:
        if expect[name] != got[name]:
            errors.append(f"{tag} denominator {name}: oracle="
                          f"{expect[name]} engine={got[name]}")
    # partition conservation
    if sum(got[k] for k in ("unobservable", "known_landed",
                            "close_undetermined", "later_known")) \
            + got["target_population"] != got["roster_points"]:
        errors.append(f"{tag}: classes do not partition the roster points")
    # metrics
    for name, errs, mb in (
            ("sched", sched_err, block["sched_one_sided"]),
            ("est", est_err, block["est_one_sided"])):
        if not errs:
            if mb is not None:
                errors.append(f"{tag}: {name} empty set reported as a metric")
            continue
        want = {"n": len(errs), "signed_error_sum_us": sum(errs),
                "abs_error_sum_us": sum(abs(x) for x in errs),
                "abs_error_p90_us": p90([abs(x) for x in errs])}
        for k, v in want.items():
            if mb[k] != v:
                errors.append(f"{tag}: {name} {k} oracle={v} engine={mb[k]}")
    if paired:
        diff = sum(abs(r["est_error_us"]) - abs(r["sched_error_us"])
                   for r in paired)
        if block["paired"]["paired_abs_diff_est_minus_sched_us"] != diff:
            errors.append(f"{tag}: paired diff mismatch")
    elif block["paired"] is not None:
        errors.append(f"{tag}: empty paired set reported as a metric")


if __name__ == "__main__":
    raise SystemExit(main())
