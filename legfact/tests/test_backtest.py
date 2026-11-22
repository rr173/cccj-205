#!/usr/bin/env python3
"""Behavioral tests for the offline arrival-prediction backtest.

Covers what the hand-computable fixture and the independent oracle cannot
assert on their own:

* frozen-input rejection: malformed JSON, bad timestamps, duplicate roots,
  non-continuous / duplicate / backwards batch numbers, non-decreasing
  visibility, coverage and close-out ordering, lead-minute validity;
* checkpoint freezing: a later warehouse schedule change never moves a
  checkpoint or re-groups a root; roster scheduled time is never used to fill
  the scheduled fact field;
* prediction hygiene: not-yet-effective updates and later-visible retroactive
  updates never enter an earlier fact; available / missing / cleared /
  conflict / stale prediction states are all produced and reported;
* evidence provenance: winning and conflict messages carry their original
  event/batch/XML location; the source warehouse is never written to;
* scale hygiene: the streaming design never scans the whole warehouse per
  sample (one SQL statement per field per distinct visible batch, plus one
  close-out answer stream).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generate_backtest_fixtures import build_warehouse  # noqa: E402
from legfact import Warehouse  # noqa: E402
from legfact.backtest_config import (BacktestInputError, BacktestInputs,  # noqa: E402
                                     load_inputs, to_us, visible_batch_at,
                                     VisibilityRow)
from legfact.backtest_engine import run_backtest  # noqa: E402
from legfact.timeutil import parse_offset_datetime  # noqa: E402

PASS = FAILS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAILS
    if cond:
        PASS += 1
    else:
        FAILS += 1
        print(f"FAIL {name}: {detail}")


def _write(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _roster_line(rid: str, root: str, group: str, sched: str) -> str:
    return json.dumps({"record_id": rid, "type": "backtest_roster_row",
                       "root_id": root, "group": group,
                       "roster_sched_arr": sched}, sort_keys=True)


def _vis_line(rid: str, bno: int, at: str) -> str:
    return json.dumps({"record_id": rid, "type": "batch_visibility",
                       "batch_no": bno, "first_visible_at": at},
                      sort_keys=True)


def _spec(**over) -> dict:
    base = {
        "airport": "CAN",
        "close_out_at": "2026-05-12T11:00:00+08:00",
        "lead_minutes": [30, 60],
        "coverage_start": "2026-05-12T07:30:00+08:00",
        "coverage_end": "2026-05-12T12:00:00+08:00",
    }
    base.update(over)
    return base


def build_env() -> tuple[str, str]:
    td = tempfile.mkdtemp(prefix="bt_test_")
    build_warehouse(td)
    wh_dir = os.path.join(td, "whdb")
    wh = Warehouse(wh_dir)
    for b in range(1, 6):
        wh.ingest_batch(os.path.join(td, "wh", f"manifest_{b}.json"),
                        os.path.join(td, "wh", f"batch_{b}"))
    wh.close()
    in_dir = os.path.join(td, "in")
    os.makedirs(in_dir)
    _write(os.path.join(in_dir, "roster.ndjson"), [
        _roster_line("RR-G1", "G1", "G1", "2026-05-12T10:30:00+08:00"),
        _roster_line("RR-G2", "G2", "G1", "2026-05-12T10:45:00+08:00"),
    ])
    _write(os.path.join(in_dir, "visibility.ndjson"), [
        _vis_line("VB-1", 1, "2026-05-12T08:00:00+08:00"),
        _vis_line("VB-2", 2, "2026-05-12T10:00:00+08:00"),
        _vis_line("VB-3", 3, "2026-05-12T10:02:00+08:00"),
        _vis_line("VB-4", 4, "2026-05-12T10:05:00+08:00"),
        _vis_line("VB-5", 5, "2026-05-12T10:30:00+08:00"),
    ])
    with open(os.path.join(in_dir, "spec.json"), "w",
              encoding="utf-8") as fh:
        json.dump(_spec(), fh)
    return td, wh_dir


def _load(td: str, wh_dir: str, *, spec=None, roster=None, vis=None):
    in_dir = os.path.join(td, "in")
    sp = spec or os.path.join(in_dir, "spec.json")
    rp = roster or os.path.join(in_dir, "roster.ndjson")
    vp = vis or os.path.join(in_dir, "visibility.ndjson")
    wh = Warehouse(wh_dir)
    try:
        max_batch = max(wh.published_batches())
        return wh, load_inputs(spec_path=sp, roster_path=rp,
                               visibility_path=vp,
                               max_published_batch=max_batch)
    except Exception:
        wh.close()
        raise


# ------------------------------------------------------------- input rejection
def test_input_rejection(td: str, wh_dir: str) -> None:
    in_dir = os.path.join(td, "in")

    def expect_reject(tag: str, *, spec=None, roster=None, vis=None):
        sp = spec or os.path.join(in_dir, "spec.json")
        rp = roster or os.path.join(in_dir, "roster.ndjson")
        vp = vis or os.path.join(in_dir, "visibility.ndjson")
        wh = Warehouse(wh_dir)
        try:
            try:
                load_inputs(spec_path=sp, roster_path=rp, visibility_path=vp,
                            max_published_batch=max(wh.published_batches()))
                check(tag, False, "no error raised")
            except BacktestInputError as exc:
                check(tag, True, exc.code)
        finally:
            wh.close()

    # malformed JSON in a line
    bad = os.path.join(in_dir, "bad.ndjson")
    _write(bad, ['{"record_id": "X", "type": "backtest_roster_row",',
                 _roster_line("RR-G1", "G1", "G1",
                              "2026-05-12T10:30:00+08:00")])
    expect_reject("malformed_json_rejected", roster=bad)

    # timestamp without an explicit offset
    badts = os.path.join(in_dir, "badts.ndjson")
    _write(badts, [_roster_line("RR-G1", "G1", "G1", "2026-05-12T10:30:00")])
    expect_reject("timestamp_without_offset_rejected", roster=badts)

    # duplicate operating root
    dup = os.path.join(in_dir, "dup.ndjson")
    _write(dup, [_roster_line("RR-1", "G1", "G1",
                              "2026-05-12T10:30:00+08:00"),
                 _roster_line("RR-2", "G1", "G1",
                              "2026-05-12T10:35:00+08:00")])
    expect_reject("duplicate_roster_root_rejected", roster=dup)

    # gap in batch numbering
    gap = os.path.join(in_dir, "gap.ndjson")
    _write(gap, [_vis_line("VB-1", 1, "2026-05-12T08:00:00+08:00"),
                 _vis_line("VB-3", 3, "2026-05-12T10:02:00+08:00")])
    expect_reject("non_continuous_batch_rejected", vis=gap)

    # duplicate batch number
    dbn = os.path.join(in_dir, "dbn.ndjson")
    _write(dbn, [_vis_line("VB-1", 1, "2026-05-12T08:00:00+08:00"),
                 _vis_line("VB-2", 1, "2026-05-12T10:00:00+08:00")])
    expect_reject("duplicate_batch_rejected", vis=dbn)

    # visibility instant going backwards
    back = os.path.join(in_dir, "back.ndjson")
    _write(back, [_vis_line("VB-1", 1, "2026-05-12T10:05:00+08:00"),
                  _vis_line("VB-2", 2, "2026-05-12T10:00:00+08:00")])
    expect_reject("visibility_order_rejected", vis=back)

    # visibility names a batch the warehouse never published
    extra = os.path.join(in_dir, "extra.ndjson")
    _write(extra, [_vis_line(f"VB-{i}", i, "2026-05-12T08:00:00+08:00")
                   for i in range(1, 7)])
    expect_reject("unpublished_batch_rejected", vis=extra)

    # invalid lead minutes (zero / float / duplicate)
    sp = os.path.join(in_dir, "spec_badlead.json")
    for bad_leads, tag in (([0], "zero_lead_rejected"),
                           ([30, 30], "duplicate_lead_rejected"),
                           ([2.5], "float_lead_rejected"),
                           ([-5], "negative_lead_rejected")):
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump(_spec(lead_minutes=bad_leads), fh)
        expect_reject(tag, spec=sp)

    # close-out before a checkpoint
    sp = os.path.join(in_dir, "spec_earlyclose.json")
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(_spec(close_out_at="2026-05-12T09:00:00+08:00"), fh)
    expect_reject("close_before_checkpoint_rejected", spec=sp)

    # checkpoint outside the claimed coverage (G1 lead-60 checkpoint is
    # 09:30, so a coverage starting at 09:31 must reject it)
    sp = os.path.join(in_dir, "spec_coverage.json")
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(_spec(coverage_start="2026-05-12T09:31:00+08:00"), fh)
    expect_reject("checkpoint_outside_coverage_rejected", spec=sp)

    # close-out outside the claimed coverage
    sp = os.path.join(in_dir, "spec_closecov.json")
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(_spec(coverage_end="2026-05-12T10:30:00+08:00"), fh)
    expect_reject("close_outside_coverage_rejected", spec=sp)


# ------------------------------------------------------------- core semantics
def test_core_semantics(td: str, wh_dir: str) -> None:
    wh, inputs = _load(td, wh_dir)
    try:
        result = run_backtest(wh, inputs)
    finally:
        wh.close()
    rows = {(r.root_id, r.lead_minutes): r for r in result["rows"]}

    # G1 lead30 = 10:00: the 10:01-effective update (batch 2) and the 10:05
    # retroactive update (batch 4) must not pollute the 10:00 ETA of 10:18.
    g1_30 = rows[("G1", 30)]
    check("g1_30_eta_is_1018",
          g1_30.est_view.value == "2026-05-12T10:18:00+08:00",
          g1_30.est_view.value)
    check("g1_30_eta_evidence_base",
          g1_30.est_view.evidence[0]["event_id"] == "E-G1-BASE")
    check("g1_30_sched_is_1030",
          g1_30.sched_view.value == "2026-05-12T10:30:00+08:00")
    check("g1_30_target", g1_30.classification == "target_population")
    check("g1_30_signed_errors",
          g1_30.sched_error_us == 600_000_000
          and g1_30.est_error_us == -120_000_000)

    # at the later G2 lead-30 checkpoint (10:15, batch 4 visible) the ETA has
    # been explicitly cleared; scheduled stays available.
    g2_30 = rows[("G2", 30)]
    check("g2_30_eta_cleared", g2_30.est_view.state == "cleared")
    check("g2_30_sched_available",
          g2_30.sched_view.state in ("value", "stale")
          and g2_30.sched_view.value == "2026-05-12T10:45:00+08:00")
    check("g2_30_eta_error_none", g2_30.est_error_us is None)
    check("g2_30_sched_error", g2_30.sched_error_us == 1_200_000_000)
    check("g2_clear_evidence",
          g2_30.est_view.evidence[0]["event_id"] == "E-G2-CLEAR")

    # conflict evidence retains BOTH contenders with their XML locations
    full_wh = Warehouse(wh_dir)
    try:
        full_inputs = _load_full(full_wh)
        full_result = run_backtest(full_wh, full_inputs)
    finally:
        full_wh.close()
    g7 = next(r for r in full_result["rows"]
              if r.root_id == "G7" and r.lead_minutes == 30)
    check("g7_close_conflict", g7.close_act_view.state == "conflict")
    ev_ids = {e["event_id"] for e in g7.close_act_view.evidence}
    check("g7_conflict_contenders",
          ev_ids == {"E-G7-ACT-A", "E-G7-ACT-B"}, str(ev_ids))
    check("g7_conflict_locations",
          all(e["xml_location"] and e["line"] for e in g7.close_act_view.evidence))
    check("g7_undetermined_conflict",
          g7.close_undetermined_reason == "actual_arrival_uncertain")

    # fractional-second error is an exact integer microsecond value
    g9 = next(r for r in full_result["rows"]
              if r.root_id == "G9" and r.lead_minutes == 30)
    check("g9_fractional_est_error", g9.est_error_us == -59_750_000,
          str(g9.est_error_us))
    check("g9_fractional_sched_error", g9.sched_error_us == 119_750_000,
          str(g9.sched_error_us))


def _load_full(wh: Warehouse) -> BacktestInputs:
    fix = os.path.join(HERE, "..", "fixtures", "backtest", "inputs")
    return load_inputs(
        spec_path=os.path.join(fix, "spec.json"),
        roster_path=os.path.join(fix, "roster.ndjson"),
        visibility_path=os.path.join(fix, "visibility.ndjson"),
        max_published_batch=max(wh.published_batches()))


def test_frozen_checkpoint_and_no_substitution(td: str, wh_dir: str) -> None:
    # A roster whose scheduled time differs from every warehouse value: the
    # checkpoint is derived from the ROSTER time, and the scheduled fact field
    # must never be filled from it.
    in_dir = os.path.join(td, "in")
    rp = os.path.join(in_dir, "roster_frozen.ndjson")
    _write(rp, [_roster_line("RR-G1", "G1", "GX",
                             "2026-05-12T10:33:00+08:00")])
    wh, inputs = _load(td, wh_dir, roster=rp)
    try:
        result = run_backtest(wh, inputs)
    finally:
        wh.close()
    r30 = next(x for x in result["rows"] if x.lead_minutes == 30)
    # checkpoint = 10:33 - 30m = 10:03, frozen from the roster regardless of
    # the warehouse's 10:30 scheduled arrival.
    check("checkpoint_frozen_from_roster",
          r30.checkpoint_us == to_us(parse_offset_datetime(
              "2026-05-12T10:03:00+08:00")))
    check("sched_field_not_filled_from_roster",
          r30.sched_view.value == "2026-05-12T10:30:00+08:00",
          r30.sched_view.value)
    check("group_label_retained", r30.group == "GX")


def test_streaming_hygiene(td: str, wh_dir: str) -> None:
    wh = Warehouse(wh_dir)
    try:
        inputs = _load_full(wh)
        stats: dict = {}
        run_backtest(wh, inputs, stats)
    finally:
        wh.close()
    # three fields, each streamed once per distinct visible batch (5), plus
    # one close-out answer stream -> bounded statements, never per sample.
    n_samples = len(inputs.roster) * len(inputs.spec.lead_minutes)
    check("sql_statements_bounded",
          stats["sql_statements"] <= 3 * 5 + 1,
          str(stats["sql_statements"]))
    check("rows_scanned_far_below_cartesian",
          stats["field_rows_scanned"] < n_samples * 3 * 5,
          str(stats["field_rows_scanned"]))


def test_visibility_helper_ties() -> None:
    vis = [
        VisibilityRow(1, "t", 100, "f", 1, "r1"),
        VisibilityRow(2, "t", 100, "f", 2, "r2"),   # same instant tie
        VisibilityRow(3, "t", 200, "f", 3, "r3"),
    ]
    check("visibility_before_first", visible_batch_at(vis, 50) == 0)
    check("visibility_tie_picks_latest", visible_batch_at(vis, 100) == 2)
    check("visibility_between", visible_batch_at(vis, 150) == 2)
    check("visibility_at_later", visible_batch_at(vis, 200) == 3)


def test_p90_rank_rule() -> None:
    from legfact.backtest_engine import _percentile_90
    vals = list(range(1, 11))           # N=10 -> ceil(9)=9th element
    check("p90_ceil_rank", _percentile_90(vals) == 9)
    vals = [5, 1, 3]                    # N=3 -> ceil(2.7)=3rd = 5
    check("p90_small_n", _percentile_90(vals) == 5)


def test_classify_precedence_and_close_reasons() -> None:
    # Exercise the fixed classification precedence and every sub-reason
    # directly, including the no-airport-root-at-close-out branch (which the
    # immutable-identity model cannot reach through a natural fixture).
    from legfact.backtest_engine import (
        _classify, _CLASS_CODE, CLS_UNOBSERVABLE, CLS_KNOWN_LANDED,
        CLS_CLOSE_UNDETERMINED, CLS_LATER_KNOWN_LANDED, CLS_TARGET,
        _AVAIL_UNAVAILABLE, _AVAIL_MISMATCH, _AVAIL_AVAILABLE,
        _CODE_VALUE, _CODE_ABSENT, UNO_NO_BATCH, UNO_ROOT_UNAVAILABLE,
        UNO_AIRPORT_MISMATCH, CLO_NO_ROOT, CLO_ACTUAL_UNCERTAIN,
        CLO_AFTER_CLOSE)

    close = 2000

    def run(cp_avail, close_avail, cp_code, cp_us, close_code, close_us):
        code, uno, clo = _classify(
            visible_batch=1, checkpoint_us=1000, close_us=close,
            cp_avail=cp_avail, close_avail=close_avail,
            cp_act_code=cp_code, cp_act_value_us=cp_us,
            close_act_code=close_code, close_act_value_us=close_us)
        return _CLASS_CODE_INV[code], uno, clo

    _CLASS_CODE_INV = {v: k for k, v in _CLASS_CODE.items()}

    # zero-batch checkpoint -> no batch visible yet
    c0, u0, _ = _classify(
        visible_batch=0, checkpoint_us=1000, close_us=close,
        cp_avail=_AVAIL_AVAILABLE, close_avail=_AVAIL_AVAILABLE,
        cp_act_code=_CODE_ABSENT, cp_act_value_us=None,
        close_act_code=_CODE_ABSENT, close_act_value_us=None)
    check("cls_no_batch_visible",
          _CLASS_CODE_INV[c0] == CLS_UNOBSERVABLE and u0 == UNO_NO_BATCH)

    check("cls_root_unavailable",
          run(_AVAIL_UNAVAILABLE, _AVAIL_AVAILABLE, _CODE_ABSENT, None,
              _CODE_ABSENT, None) == (CLS_UNOBSERVABLE,
                                      UNO_ROOT_UNAVAILABLE, None))
    check("cls_airport_mismatch",
          run(_AVAIL_MISMATCH, _AVAIL_AVAILABLE, _CODE_ABSENT, None,
              _CODE_ABSENT, None) == (CLS_UNOBSERVABLE,
                                      UNO_AIRPORT_MISMATCH, None))
    check("cls_known_landed",
          run(_AVAIL_AVAILABLE, _AVAIL_AVAILABLE, _CODE_VALUE, 900,
              _CODE_VALUE, 900) == (CLS_KNOWN_LANDED, None, None))
    # no airport root at close-out (after the checkpoint itself was observable)
    check("cls_close_no_root",
          run(_AVAIL_AVAILABLE, _AVAIL_UNAVAILABLE, _CODE_ABSENT, None,
              _CODE_ABSENT, None) == (CLS_CLOSE_UNDETERMINED, None,
                                      CLO_NO_ROOT))
    check("cls_close_uncertain",
          run(_AVAIL_AVAILABLE, _AVAIL_AVAILABLE, _CODE_ABSENT, None,
              _CODE_ABSENT, None) == (CLS_CLOSE_UNDETERMINED, None,
                                      CLO_ACTUAL_UNCERTAIN))
    check("cls_close_after",
          run(_AVAIL_AVAILABLE, _AVAIL_AVAILABLE, _CODE_ABSENT, None,
              _CODE_VALUE, 2500) == (CLS_CLOSE_UNDETERMINED, None,
                                     CLO_AFTER_CLOSE))
    check("cls_later_known_landed",
          run(_AVAIL_AVAILABLE, _AVAIL_AVAILABLE, _CODE_ABSENT, None,
              _CODE_VALUE, 1000) == (CLS_LATER_KNOWN_LANDED, None, None))
    check("cls_target",
          run(_AVAIL_AVAILABLE, _AVAIL_AVAILABLE, _CODE_ABSENT, None,
              _CODE_VALUE, 1500) == (CLS_TARGET, None, None))


def test_no_writeback(td: str, wh_dir: str) -> None:
    db = os.path.join(wh_dir, "legfact.sqlite")
    before = os.path.getmtime(db)
    wh = Warehouse(wh_dir)
    try:
        run_backtest(wh, _load_full(wh))
    finally:
        wh.close()
    check("source_warehouse_not_rewritten", os.path.getmtime(db) == before)


def main() -> int:
    td, wh_dir = build_env()
    try:
        test_input_rejection(td, wh_dir)
        test_core_semantics(td, wh_dir)
        test_frozen_checkpoint_and_no_substitution(td, wh_dir)
        test_streaming_hygiene(td, wh_dir)
        test_visibility_helper_ties()
        test_p90_rank_rule()
        test_classify_precedence_and_close_reasons()
        test_no_writeback(td, wh_dir)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"backtest behavioral tests: {PASS} passed, {FAILS} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
