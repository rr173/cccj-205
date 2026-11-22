#!/usr/bin/env python3
"""Runtime scale rehearsal for the offline arrival-prediction backtest.

Synthesizes, into a throwaway directory, a warehouse and frozen inputs at the
required audit scale:

* at least 60,000 operating roots, each with FOUR checkpoints (lead minutes);
* at least 72 published knowledge batches with a non-decreasing duty-system
  visibility table spread across one operating day;
* at least 400,000 sparse fact messages, where most batches only touch a small
  number of roots;
* a set of repeatedly-corrected HOT roots (estimates revised in many batches),
  plus roots whose identity association only closes in a later batch.

Nothing generated here is committed: only the small fixtures and this
generator live in the repository.  The backtest runs in a SEPARATE process so
the reported peak RSS (VmHWM) is the calculation job's own high-water mark,
and sample rows stream to disk rather than accumulating in memory.

Run:  python3 -I tests/scale_backtest.py [--roots 60000] [--batches 72]
          [--messages 400000]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generate_fixtures import aidx_xml, identity_file, rel_initial, write_batch  # noqa: E402

AIRPORT = "CAN"
AUTH = "CAN-SCALE"
DAY = "2026-06-15"
TZ = timezone(timedelta(hours=8))
LEADS = [30, 60, 90, 120]               # exactly four checkpoints per root
N_GROUPS = 12
HOT_ROOTS = 2000                        # repeatedly corrected hotspots
DELAYED_ROOTS = 300                     # identity closes in a later batch
DAY_START = datetime(2026, 6, 15, 6, 0, 0, tzinfo=TZ)
# A backtest single-process RSS ceiling (the audit requirement).
RSS_LIMIT_MB = int(os.environ.get("BACKTEST_RSS_LIMIT_MB", "512"))
RSS_TARGET_MB = 512
MIN_US = 60 * 1_000_000


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


def _key(fno: str) -> dict:
    return {"carrier": "CA", "flight_no": fno, "suffix": "",
            "service_date": DAY, "departure": "PEK", "arrival": AIRPORT}


def _msg(event_id, fno, *, eff, times, rev=1, ident=None):
    return aidx_xml(
        event_id=event_id, sender=AUTH, revision=rev,
        effective_at=_iso(eff), identity_at=_iso(ident or DAY_START),
        carrier="CA", flight_no=fno, suffix="", service_date=DAY,
        dep="PEK", arr=AIRPORT, times=times), "aidx_xml"


# deterministic LCG so the generator never depends on global random state
class _Rng:
    def __init__(self, seed: int = 0xBADC0DE):
        self.state = seed & ((1 << 64) - 1)

    def next(self) -> int:
        self.state = (self.state * 6364136223846793005
                      + 1442695040888963407) & ((1 << 64) - 1)
        return self.state >> 33


# --------------------------------------------------------------- generation
def generate(td: str, n_roots: int, n_batches: int,
             n_messages: int) -> dict:
    rng = _Rng()
    wh_src = os.path.join(td, "wh_src")
    # Scale the hotspot set down for small dry runs; it never exceeds roots.
    hot_count = min(HOT_ROOTS, max(1, n_roots // 10))
    delayed_count = min(DELAYED_ROOTS, max(1, n_roots // 20))
    # Roster scheduled arrivals are spread over 08:00..20:00 local.
    day_span = 12 * 3600
    sched = {}
    for i in range(n_roots):
        sec = (i * 397) % day_span          # deterministic even spread
        sched[i] = DAY_START + timedelta(seconds=sec) + timedelta(hours=2)

    # Batch visibility instants: batch 1 at 06:00, then evenly across the day.
    vis_span = (datetime(2026, 6, 15, 18, 0, tzinfo=TZ) - DAY_START).total_seconds()
    visibility = []
    for b in range(1, n_batches + 1):
        if b == 1:
            at = DAY_START
        else:
            at = DAY_START + timedelta(
                seconds=int(vis_span * (b - 1) / (n_batches - 1)))
        visibility.append((b, at))
    vis_at = dict(visibility)

    # ---- declare roots.  Most roots exist from batch 1; the delayed roots
    # are declared only in a later batch, exercising late identity closure.
    delayed_set = set(range(n_roots - delayed_count, n_roots))
    delayed_close_batch = {i: 2 + (i % max(1, n_batches - 2))
                           for i in delayed_set}
    rels_b1 = [
        rel_initial(f"IL-R{i}", f"R{i}", _key(str(700000 + i)),
                    _iso(DAY_START), AUTH, f"J{i}")
        for i in range(n_roots) if i not in delayed_set]
    files_b1 = {"identity_b1.json": (identity_file(rels_b1), "identity_json")}

    # Every root gets a baseline scheduled + estimated arrival in batch 1
    # (delayed roots receive their baseline in their closing batch).
    def baseline_message(i: int, eff: datetime):
        sa = sched[i]
        ea = sa + timedelta(seconds=300 + (rng.next() % 1800))
        return _msg(f"E-R{i}-BASE", str(700000 + i), eff=eff,
                    times={"sched_arr": _iso(sa), "est_arr": _iso(ea)})

    for i in range(n_roots):
        if i in delayed_set:
            continue
        files_b1[f"msg_r{i}_base.xml"] = baseline_message(i, DAY_START)
    write_batch(wh_src, 1, AUTH, files_b1)

    # ---- plan the remaining messages to hit the sparse budget exactly.
    # Structure: one baseline + one actual per root (2*n_roots), then a
    # deterministic update budget split between repeatedly-corrected hot
    # roots and sparse cold-root updates spread across all batches.
    plan: dict[int, list[tuple]] = {}     # batch -> list of event payloads
    n_baseline = n_roots                  # one base message per root
    n_actual = n_roots                    # one actual per root
    update_budget = max(0, n_messages - n_baseline - n_actual)
    hot = set(range(hot_count))
    cold = [i for i in range(n_roots) if i not in hot]

    def schedule(batch: int, event_id: str, i: int, kind: str,
                 eff: datetime, rev: int, times: dict):
        # Make the on-disk filename/event id globally unique by suffixing the
        # batch and a per-batch ordinal, so two messages for the same root
        # landing in one batch can never silently overwrite each other.
        ordinal = len(plan.setdefault(batch, []))
        unique_id = f"{event_id}-b{batch}-n{ordinal}"
        plan[batch].append(
            (unique_id, str(700000 + i), eff, rev, times))

    def first_batch(i: int) -> int:
        """Earliest batch in which root i may carry a message."""
        return delayed_close_batch[i] if i in delayed_set else 1

    # ---- hot roots: R full correction rounds, each round visiting every hot
    # root once, rotated across batches so a single batch only touches a
    # minority of roots even for the hot population.
    hot_rounds = 8
    hot_updates = min(update_budget, hot_count * hot_rounds)
    hot_done = 0
    for rnd in range(hot_rounds):
        for i in hot:
            if hot_done >= update_budget:
                break
            fb = first_batch(i)
            if fb >= n_batches:
                continue
            # round rnd maps this root to a rotating later batch
            batch = fb + ((rnd * 7 + i) % max(1, n_batches - fb))
            batch = min(batch, n_batches)
            eff = vis_at[batch] + timedelta(seconds=30)
            ea = sched[i] + timedelta(seconds=((rng.next() % 3600) - 1200))
            schedule(batch, f"E-R{i}-EST-r{rnd}", i, "EST", eff,
                     rev=100 + rnd, times={"est_arr": _iso(ea)})
            hot_done += 1

    # ---- cold roots: spread the remaining update budget evenly so the count
    # is deterministic and every cold root gets at least one later update.
    remaining = update_budget - hot_done
    per_cold = remaining // max(1, len(cold))
    extra = remaining % max(1, len(cold))
    cold_done = 0
    for idx, i in enumerate(cold):
        n_updates = per_cold + (1 if idx < extra else 0)
        fb = first_batch(i)
        for u in range(n_updates):
            if fb > n_batches:
                break
            batch = fb + ((i * 7 + u * 13) % max(1, n_batches - fb))
            eff = vis_at[batch] + timedelta(seconds=20)
            kind = "EST" if u % 2 == 0 else "SCHED"
            field = "est_arr" if kind == "EST" else "sched_arr"
            val = sched[i] + timedelta(seconds=((rng.next() % 2400) - 900))
            schedule(batch, f"E-R{i}-{kind}-{u}", i, kind, eff,
                     rev=200 + u, times={field: _iso(val)})
            cold_done += 1

    # ---- delayed roots' baselines land in their identity-closing batch
    # (their batch-1 absence is what makes the early checkpoints unobservable).
    for i in delayed_set:
        cb = delayed_close_batch[i]
        schedule(cb, f"E-R{i}-BASE", i, "BASE",
                 vis_at[cb] + timedelta(seconds=10), 1,
                 {"sched_arr": _iso(sched[i]),
                  "est_arr": _iso(sched[i] + timedelta(seconds=600))})

    # ---- actual arrivals: one per root in a late but close-out-visible
    # batch; a few actuals are future-dated past the close-out instant.
    close_out = datetime(2026, 6, 15, 20, 0, tzinfo=TZ)
    for i in range(n_roots):
        fb = first_batch(i)
        batch = min(n_batches, max(fb, n_batches - (i % 5)))
        actual = sched[i] + timedelta(seconds=((rng.next() % 1800) - 600))
        schedule(batch, f"E-R{i}-ACT", i, "ACT",
                 vis_at[batch] + timedelta(seconds=40), rev=1,
                 times={"act_arr": _iso(actual)})

    # ---- guaranteed top-up so the emitted count actually reaches the
    # requested budget even when some cold updates were skipped (delayed roots
    # whose only eligible batch is the last one).  The true eventual emitted
    # total is the batch-1 non-delayed baselines plus every planned event;
    # top-up runs until that total reaches the requested budget.
    target_total = n_messages
    topup = 0
    eligible = [i for i in range(n_roots) if first_batch(i) <= n_batches]

    def projected_total() -> int:
        # Mirror the real emit logic exactly: batch-1 baselines (non-delayed
        # roots) plus only the plan events that land in batches 2..n.  Any
        # plan entry keyed to batch 1 would never be emitted and is excluded.
        planned_emittable = sum(
            len(v) for b, v in plan.items() if b >= 2)
        return (n_roots - len(delayed_set)) + planned_emittable

    j = 0
    while eligible:
        if projected_total() >= target_total:
            break
        i = eligible[j % len(eligible)]
        fb = first_batch(i)
        # top-ups always land in batches 2..n (batch 1 is emitted separately),
        # so clamp the earliest usable batch to at least 2.
        lo = max(2, fb + 1)
        if lo > n_batches:
            j += 1
            continue
        span = max(1, n_batches - lo + 1)
        batch = lo + ((j * 7) % span)
        val = sched[i] + timedelta(seconds=((rng.next() % 2400) - 900))
        schedule(batch, f"E-R{i}-TOP-{topup}", i, "EST",
                 vis_at[batch] + timedelta(seconds=25), 300 + topup,
                 {"est_arr": _iso(val)})
        topup += 1
        j += 1

    # Plan-total metadata; the authoritative emitted count is recomputed while
    # writing the batches below (n_emitted), which cannot double-count.
    written = projected_total()

    # ---- emit batches 2..n; delayed identity declarations ride their batch
    n_emitted = len(files_b1) - 1     # batch-1 baseline XML messages
    for b in range(2, n_batches + 1):
        files = {}
        closing = [i for i in delayed_set if delayed_close_batch[i] == b]
        if closing:
            rels = [rel_initial(
                f"IL-R{i}", f"R{i}", _key(str(700000 + i)),
                _iso(DAY_START), AUTH, f"J{i}") for i in closing]
            files[f"identity_b{b}.json"] = (identity_file(rels),
                                            "identity_json")
        for event_id, fno, eff, rev, times in plan.get(b, []):
            files[event_id + ".xml"] = _msg(
                event_id, fno, eff=eff, rev=rev, times=times)
            n_emitted += 1
        if files:
            write_batch(wh_src, b, AUTH, files)
        else:
            # every batch number must exist for the continuous visibility
            write_batch(wh_src, b, AUTH,
                        {f"identity_b{b}_empty.json":
                         (identity_file([]), "identity_json")})

    # ---- frozen inputs
    in_dir = os.path.join(td, "inputs")
    os.makedirs(in_dir, exist_ok=True)
    with open(os.path.join(in_dir, "roster.ndjson"), "w",
              encoding="utf-8") as fh:
        for i in range(n_roots):
            rec = {"record_id": f"RR-{i}", "type": "backtest_roster_row",
                   "root_id": f"R{i}", "group": f"G{i % N_GROUPS}",
                   "roster_sched_arr": _iso(sched[i])}
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    with open(os.path.join(in_dir, "visibility.ndjson"), "w",
              encoding="utf-8") as fh:
        for bno, at in visibility:
            fh.write(json.dumps({
                "record_id": f"VB-{bno}", "type": "batch_visibility",
                "batch_no": bno, "first_visible_at": _iso(at)},
                sort_keys=True) + "\n")
    spec = {
        "airport": AIRPORT,
        "close_out_at": _iso(close_out),
        "lead_minutes": LEADS,
        "coverage_start": _iso(DAY_START),
        "coverage_end": _iso(datetime(2026, 6, 15, 21, 0, tzinfo=TZ)),
    }
    with open(os.path.join(in_dir, "spec.json"), "w",
              encoding="utf-8") as fh:
        json.dump(spec, fh)
    return {
        "wh_src": wh_src, "inputs": in_dir,
        "roster": os.path.join(in_dir, "roster.ndjson"),
        "visibility": os.path.join(in_dir, "visibility.ndjson"),
        "spec": os.path.join(in_dir, "spec.json"),
        "n_roots": n_roots, "n_batches": n_batches,
        "messages_written": n_emitted, "hot_roots": hot_count,
        "delayed_roots": delayed_count, "n_groups": N_GROUPS,
        "messages_planned": written,
        "checkpoints": n_roots * len(LEADS),
    }


def _peak_rss_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# ------------------------------------------------------------------- workers
def worker_gen(args) -> dict:
    td = tempfile.mkdtemp(prefix="bt_scale_")
    info = generate(td, args.roots, args.batches, args.messages)
    from legfact import Warehouse
    wh_dir = os.path.join(td, "whdb")
    wh = Warehouse(wh_dir)
    t = time.time()
    for b in range(1, args.batches + 1):
        wh.ingest_batch(os.path.join(info["wh_src"], f"manifest_{b}.json"),
                        os.path.join(info["wh_src"], f"batch_{b}"))
    wh.close()
    info.update(tmpdir=td, warehouse=wh_dir,
                ingest_seconds=round(time.time() - t, 1),
                build_peak_rss_mb=round(_peak_rss_mb(), 1))
    return info


def worker_calc(info: dict) -> dict:
    from legfact import Warehouse
    from legfact.backtest_config import load_inputs
    from legfact.backtest_engine import run_backtest_streaming, NdjsonFileSink
    wh = Warehouse(info["warehouse"])
    max_batch = max(wh.published_batches())
    inputs = load_inputs(
        spec_path=info["spec"], roster_path=info["roster"],
        visibility_path=info["visibility"], max_published_batch=max_batch)
    out_dir = os.path.join(info["tmpdir"], "backtest_out")
    os.makedirs(out_dir, exist_ok=True)
    sink = NdjsonFileSink(os.path.join(out_dir, "samples.ndjson"))
    stats: dict = {}
    t = time.time()
    result = run_backtest_streaming(wh, inputs, sink, stats)
    calc_s = time.time() - t
    sink.close()
    with open(os.path.join(out_dir, "summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(result["summary"], fh)
    wh.close()
    peak = _peak_rss_mb()

    # ---- independent conservation re-check from the streamed detail file
    class_counts = result["summary"]["conservation"]["class_counts"]
    total = result["summary"]["conservation"]["total_rows"]
    n_detail = sum(1 for _ in open(sink.path, encoding="utf-8"))
    # overall per-lead denominators must equal the roster x leads layout
    overall = result["summary"]["overall_per_lead"]
    denom_ok = (
        n_detail == total == info["checkpoints"]
        and sum(class_counts.values()) == total
        and all(b["roster_points"] == info["n_roots"] for b in overall)
        and result["summary"]["conservation"]["checks"][
            "all_conservation_ok"])
    # P90 computed from whole-population samples, not averaged across groups
    p90_ok = all(
        b["sched_one_sided"] is None
        or isinstance(b["sched_one_sided"]["abs_error_p90_us"], int)
        for b in overall)
    return {
        "calc_seconds": round(calc_s, 1),
        "samples_written": sink.count,
        "detail_lines": n_detail,
        "stream_stats": stats,
        "class_counts": class_counts,
        "denominator_conservation_ok": denom_ok,
        "overall_p90_integer_ok": p90_ok,
        "calc_peak_rss_mb": round(peak, 1),
        "rss_target_mb": RSS_TARGET_MB,
        "meets_512mib_target": peak < RSS_TARGET_MB,
        "sidecar_dir_bytes": sum(
            os.path.getsize(os.path.join(out_dir, f))
            for f in os.listdir(out_dir)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", type=int, default=60_000)
    ap.add_argument("--batches", type=int, default=72)
    ap.add_argument("--messages", type=int, default=400_000)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--worker", choices=("gen", "calc"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--paths", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker == "gen":
        print(json.dumps(worker_gen(args)))
        return 0
    if args.worker == "calc":
        info = json.load(open(args.paths, encoding="utf-8"))
        print(json.dumps(worker_calc(info), indent=2))
        return 0

    t0 = time.time()
    gen = subprocess.run(
        [sys.executable, "-I", os.path.abspath(__file__), "--worker", "gen",
         "--roots", str(args.roots), "--batches", str(args.batches),
         "--messages", str(args.messages)],
        capture_output=True, text=True, check=True)
    info = json.loads(gen.stdout)
    paths = os.path.join(info["tmpdir"], "paths.json")
    with open(paths, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    calc_env = dict(os.environ)
    # Route allocations through libc malloc and return freed arenas promptly,
    # so the streaming design's live working set — not retained pymalloc
    # arenas — determines the RSS high-water mark.
    calc_env["PYTHONMALLOC"] = "malloc"
    calc_env["MALLOC_TRIM_THRESHOLD_"] = "65536"
    calc = subprocess.run(
        [sys.executable, "-I", os.path.abspath(__file__), "--worker", "calc",
         "--paths", paths], capture_output=True, text=True, check=True,
        env=calc_env)
    report = json.loads(calc.stdout)
    ok = (report["denominator_conservation_ok"]
          and report["overall_p90_integer_ok"]
          and report["calc_peak_rss_mb"] < RSS_LIMIT_MB
          and report["samples_written"] == info["checkpoints"]
          and info["messages_written"] >= args.messages
          and report["stream_stats"]["sql_statements"]
          <= 3 * args.batches + 1)
    out = {
        "scenario": {"roots": args.roots, "batches": args.batches,
                     "messages_target": args.messages,
                     "messages_written": info["messages_written"],
                     "checkpoints_per_root": len(LEADS),
                     "total_checkpoints": info["checkpoints"],
                     "hot_roots": info["hot_roots"],
                     "delayed_roots": info["delayed_roots"],
                     "groups": info["n_groups"]},
        "wall_seconds": round(time.time() - t0, 1),
        "ingest_seconds": info["ingest_seconds"],
        "build_peak_rss_mb": info["build_peak_rss_mb"],
        **report,
        "rss_limit_mb": RSS_LIMIT_MB,
        "pass": ok,
    }
    print(json.dumps(out, indent=2))
    if not args.keep:
        shutil.rmtree(info["tmpdir"], ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
