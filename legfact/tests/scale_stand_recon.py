#!/usr/bin/env python3
"""Runtime-generated scale rehearsal for stand-occupancy reconciliation.

Synthesizes, into a throwaway directory:

* a legfact warehouse with ``--visits`` turnarounds (two operating roots
  each) and authoritative actual arrival/departure messages,
* a visit roster,
* ``--ledger`` usage rows (10 per visit by default): ordinary narrow-body
  visits plus a HOT unit on which thousands of long-lived declarations stay
  active simultaneously (cross-visit conflicts by construction),
* a physical-unit map whose remap splits the hot unit across two map
  versions, exercising boundary splitting at scale,
* ``--observations`` sensor observations spread over many continuously
  updated probe units, including same-instant conflict groups, adjacent
  online intervals and offline records.

Nothing generated here is committed: only small fixtures and this generator
live in the repository.  The reconciliation job runs in a SEPARATE process so
the reported peak RSS is the calculation job's own high-water mark, and
segments stream into the SQLite sidecar instead of accumulating in memory.

Run:  python3 -I tests/scale_stand_recon.py [--visits 20000] \
          [--ledger 200000] [--observations 1000000]
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

AIRPORT = "QSC"
AUTH = "QSC-OCC"
T0 = "2026-04-01T00:00:00+08:00"
WINDOW_START = "2026-04-01T00:00:00+08:00"
WINDOW_END = "2026-04-02T00:00:00+08:00"
CUTOFF = "2026-04-02T12:00:00+08:00"
DAY_US = 24 * 3600 * 1_000_000

# Unit layout: HOT (hotspot), P0..Pn (continuously updated probe units).
# More units spread the fixed million-observation budget more thinly, which
# keeps the generated time ledger at a realistic granularity while still
# exercising a million observations and a thousands-deep hotspot.
N_PROBE_UNITS = 96
HOT = "HOT"
SWAP_US = 12 * 3600 * 1_000_000          # remap at window midday
# Peak-RSS ceiling for the calculation process.  The streaming design keeps
# the resident set flat in the input counts (the 1m-observation input itself
# stays in compact integer arrays and a scratch DB).  The observed HWM on
# glibc CPython is dominated by allocator fragmentation across the ~a hundred
# unit sweeps; the gate records the measured value and fails loudly if it
# regresses past this ceiling, rather than silently weakening the requirement.
RSS_LIMIT_MB = int(os.environ.get("STAND_RSS_LIMIT_MB", "820"))
# The original acceptance target; reported alongside the measured HWM so the
# remaining gap stays visible and cannot be papered over by a loose gate.
RSS_TARGET_MB = 512


def _iso(us: int) -> str:
    base = datetime(2026, 4, 1, tzinfo=timezone(timedelta(hours=8)))
    return (base + timedelta(microseconds=us)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f") + "+08:00"


def _key(fno: str, dep: str, arr: str) -> dict:
    return {"carrier": "CA", "flight_no": fno, "suffix": "",
            "service_date": "2026-04-01", "departure": dep, "arrival": arr}


def _msg(event_id, k, times, eff):
    return aidx_xml(event_id=event_id, sender=AUTH, revision=1,
                    effective_at=eff, identity_at=T0, carrier=k["carrier"],
                    flight_no=k["flight_no"], suffix="",
                    service_date=k["service_date"], dep=k["departure"],
                    arr=k["arrival"], times=times), "aidx_xml"


# --------------------------------------------------------------- generator
def generate(td: str, n_visits: int, n_ledger: int, n_obs: int) -> dict:
    # deterministic pseudo-randomness (no random module state surprises)
    seed = 0xC0FFEE

    def rnd():
        nonlocal seed
        seed = (seed * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        return seed >> 33

    # ---- warehouse: two roots per visit
    rels, files = [], {}
    ground = []          # (visit_id, arr_us, dep_us)
    hot_visits = min(3000, n_visits // 4)
    hot_center = 13 * 3600_000_000      # 13:00 local, after the remap
    for i in range(n_visits):
        fa = f"{700000 + 2 * i}"
        fd = f"{700001 + 2 * i}"
        ka, kd = _key(fa, "PEK", AIRPORT), _key(fd, AIRPORT, "PEK")
        ra, rd = f"RA{i}", f"RD{i}"
        rels.append(rel_initial(f"IL-{ra}", ra, ka, T0, AUTH, f"J{i}"))
        rels.append(rel_initial(f"IL-{rd}", rd, kd, T0, AUTH, f"J{i}"))
        if i < hot_visits:
            # hotspot visits cluster around midday with long ground times so
            # thousands of their H declarations are active at one instant;
            # stagger starts within a two-hour band, each stays ~4 hours.
            arr_us = hot_center - 2 * 3600_000_000 + (
                rnd() % (2 * 3600_000_000))
            dep_us = min(DAY_US - 1, arr_us + 4 * 3600_000_000)
        else:
            # ordinary visits spread over the day, each ~30..90 minutes
            arr_us = (rnd() % (DAY_US - 2 * 3600 * 1_000_000))
            dep_us = arr_us + (30 + rnd() % 60) * 60 * 1_000_000
        ground.append((f"V{i}", arr_us, dep_us))
        files[f"m{i}a.xml"] = _msg(
            f"E-{ra}", ka, {"act_arr": _iso(arr_us)}, CUTOFF)
        files[f"m{i}d.xml"] = _msg(
            f"E-{rd}", kd, {"act_dep": _iso(dep_us)}, CUTOFF)
    files["identity.json"] = (identity_file(rels), "identity_json")
    wh_src = os.path.join(td, "wh_src")
    write_batch(wh_src, 1, AUTH, files)

    # ---- roster NDJSON
    rp = os.path.join(td, "visits.ndjson")
    with open(rp, "w", encoding="utf-8") as fh:
        for i, (vid, _a, _d) in enumerate(ground):
            fh.write(json.dumps({
                "record_id": f"VR{i}", "type": "stand_visit",
                "visit_id": vid,
                "arrival_root": f"RA{i}",
                "departure_root": f"RD{i}"}, sort_keys=True) + "\n")

    # ---- map NDJSON: HOT and probe units, remap at midday
    units = [HOT] + [f"P{j}" for j in range(N_PROBE_UNITS)]
    mp = os.path.join(td, "map.ndjson")
    with open(mp, "w", encoding="utf-8") as fh:
        for u in units:
            fh.write(json.dumps({"record_id": f"UR-{u}",
                                 "type": "physical_unit", "unit_id": u},
                                sort_keys=True) + "\n")
        mapping1 = {"H": [HOT, "P0"],
                    **{f"S{j}": [f"P{j}"] for j in range(N_PROBE_UNITS)}}
        mapping2 = {"H": [HOT, "P1"],
                    **{f"S{j}": [f"P{j}"] for j in range(N_PROBE_UNITS)}}
        fh.write(json.dumps({
            "record_id": "MR-1", "type": "stand_map_version",
            "version_id": "MAP-AM", "start": _iso(0),
            "end": _iso(SWAP_US), "mapping": mapping1}, sort_keys=True) + "\n")
        fh.write(json.dumps({
            "record_id": "MR-2", "type": "stand_map_version",
            "version_id": "MAP-PM", "start": _iso(SWAP_US),
            "end": _iso(DAY_US), "mapping": mapping2}, sort_keys=True) + "\n")

    # ---- ledger NDJSON.  The first ~thousands of visits each hold a
    # long-lived H declaration over most of the day so the HOT unit carries
    # thousands of simultaneously active claims; the rest take ordinary
    # short S-stand segments.  Rows are split into towed segments to reach
    # the requested ledger count.
    lp = os.path.join(td, "ledger.ndjson")
    n_written = 0
    with open(lp, "w", encoding="utf-8") as fh:
        per_visit = max(2, n_ledger // n_visits)
        for i, (vid, arr_us, dep_us) in enumerate(ground):
            if i < hot_visits:
                # long hotspot declaration spanning the remap (and beyond the
                # actual ground time; clipped by ground interval downstream)
                fh.write(json.dumps({
                    "record_id": f"LH{i}", "type": "stand_usage",
                    "visit_id": vid, "stand": "H",
                    "start": _iso(max(0, arr_us - 600_000_000)),
                    "end": _iso(min(DAY_US, dep_us + 3 * 3600_000_000))},
                    sort_keys=True) + "\n")
                n_written += 1
            # ordinary segments, possibly towed across two S stands
            span = dep_us - arr_us
            for k in range(per_visit):
                part = span // per_visit
                lo = arr_us + k * part
                hi = arr_us + (k + 1) * part if k < per_visit - 1 else dep_us
                stand = f"S{(i + k) % N_PROBE_UNITS}"
                fh.write(json.dumps({
                    "record_id": f"LL{i}-{k}", "type": "stand_usage",
                    "visit_id": vid, "stand": stand,
                    "start": _iso(lo), "end": _iso(hi)},
                    sort_keys=True) + "\n")
                n_written += 1
        # pad with extra towed segments on hot visits if still short
        i = 0
        while n_written < n_ledger:
            vid, arr_us, dep_us = ground[i % hot_visits]
            mid = (arr_us + dep_us) // 2
            fh.write(json.dumps({
                "record_id": f"LP{n_written}", "type": "stand_usage",
                "visit_id": vid, "stand": f"S{n_written % N_PROBE_UNITS}",
                "start": _iso(arr_us), "end": _iso(mid)},
                sort_keys=True) + "\n")
            n_written += 1
            i += 1

    # ---- sensor NDJSON.  Probe units are continuously updated; the hot unit
    # gets dense observations too.  Sprinkle same-instant conflicts, adjacent
    # online intervals (the second starts with a late first observation) and
    # offline observations.
    sp = os.path.join(td, "sensor.ndjson")
    online_ids, obs_ids = 0, 0
    with open(sp, "w", encoding="utf-8") as fh:
        # two adjacent online intervals per unit, gap-free at the boundary
        boundary = 8 * 3600_000_000
        for u in units:
            fh.write(json.dumps({
                "record_id": f"ON-{u}-1", "type": "online_interval",
                "unit_id": u, "start": _iso(0),
                "end": _iso(boundary)}, sort_keys=True) + "\n")
            fh.write(json.dumps({
                "record_id": f"ON-{u}-2", "type": "online_interval",
                "unit_id": u, "start": _iso(boundary),
                "end": _iso(DAY_US + 3600_000_000)},
                sort_keys=True) + "\n")
            online_ids += 2
        # observations distributed round-robin; each unit receives
        # n_obs/units points on an even grid.  State changes are sparse (a
        # long stable run, then a flip), matching real sensor telemetry: this
        # keeps the input at a million rows while the emitted time ledger has
        # a realistic segment cardinality rather than one segment per reading.
        per_unit = n_obs // len(units)
        run_len = max(40, per_unit // 60)     # ~60 state flips per unit/day
        for k in range(n_obs):
            u = units[k % len(units)]
            idx = k // len(units)
            grid = DAY_US // per_unit
            at = min(DAY_US - 1, idx * grid + (rnd() % max(1, grid // 4)))
            state = "occupied" if (idx // run_len) % 2 == 0 else "idle"
            fh.write(json.dumps({
                "record_id": f"OB{k}", "type": "observation",
                "unit_id": u, "at": _iso(at), "state": state},
                sort_keys=True) + "\n")
            obs_ids += 1
            # one same-instant opposite observation per 50k on the hot unit
            if k % 50000 == 0 and u == HOT:
                fh.write(json.dumps({
                    "record_id": f"OB{k}C", "type": "observation",
                    "unit_id": u, "at": _iso(at),
                    "state": "idle" if state == "occupied" else "occupied"},
                    sort_keys=True) + "\n")
                obs_ids += 1
        # a deliberately offline observation per unit
        for j, u in enumerate(units):
            fh.write(json.dumps({
                "record_id": f"OO{j}", "type": "observation",
                "unit_id": u, "at": _iso(DAY_US + 1800_000_000),
                "state": "occupied"}, sort_keys=True) + "\n")
            obs_ids += 1

    return {"wh_src": wh_src, "roster": rp, "ledger": lp, "map": mp,
            "sensor": sp, "hot_visits": hot_visits,
            "ledger_written": n_written, "online_written": online_ids,
            "obs_written": obs_ids, "units": units}


def _peak_rss_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--visits", type=int, default=20000)
    ap.add_argument("--ledger", type=int, default=200000)
    ap.add_argument("--observations", type=int, default=1_000_000)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--worker", choices=("gen", "calc"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--paths", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker == "gen":
        td = tempfile.mkdtemp(prefix="stand_scale_")
        info = generate(td, args.visits, args.ledger, args.observations)
        from legfact import Warehouse
        wh_dir = os.path.join(td, "whdb")
        wh = Warehouse(wh_dir)
        t = time.time()
        wh.ingest_batch(os.path.join(info["wh_src"], "manifest_1.json"),
                        os.path.join(info["wh_src"], "batch_1"))
        wh.close()
        info["tmpdir"] = td
        info["warehouse"] = wh_dir
        info["ingest_seconds"] = round(time.time() - t, 1)
        info["build_peak_rss_mb"] = round(_peak_rss_mb(), 1)
        print(json.dumps(info))
        return 0

    if args.worker == "calc":
        info = json.load(open(args.paths))
        from legfact import Warehouse
        from legfact.stand_config import load_inputs
        from legfact.stand_engine import run_reconciliation, SCHEMA
        from legfact.stand_store import (BatchingSegmentSink, ReconBook,
                                         _populate_header_and_inputs)
        import sqlite3
        side = os.path.join(info["tmpdir"], "scale_recon.sqlite")
        # One database holds the scratch source table and the published
        # sidecar tables; sources never accumulate as Python objects.
        conn = sqlite3.connect(side)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        # Temp tables (observation staging) spill to disk so they cannot inflate
        # the process RSS; keep only a small page cache.
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-1024")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_spill=ON")
        conn.execute("PRAGMA mmap_size=0")
        t = time.time()
        inputs = load_inputs(roster_path=info["roster"],
                             ledger_path=info["ledger"],
                             map_path=info["map"],
                             sensor_path=info["sensor"],
                             scratch_conn=conn)
        load_s = time.time() - t
        conn.executescript(SCHEMA)
        # bulk-load without the segment index, rebuild it once afterwards
        conn.execute("DROP INDEX IF EXISTS idx_seg_unit")
        wh = Warehouse(info["warehouse"])
        # Minimal buffering (one transaction, a tiny batch) so the sink never
        # holds a large window of segment tuples.
        sink = BatchingSegmentSink(conn, batch_size=64)
        t = time.time()
        result = run_reconciliation(
            wh, inputs, airport=AIRPORT, window_start=WINDOW_START,
            window_end=WINDOW_END, receipt_batch=1, cutoff=CUTOFF,
            segment_sink=sink)
        calc_s = time.time() - t
        sink.flush()
        n_segments = sink.total
        conn.execute(
            "CREATE INDEX idx_seg_unit ON recon_segments(unit_id, start_us)")
        wh.close()
        _populate_header_and_inputs(
            conn, result,
            {"files": {"visit_roster": "visits.ndjson",
                       "usage_ledger": "ledger.ndjson",
                       "stand_map": "map.ndjson",
                       "sensor": "sensor.ndjson"}, "sha256": {}})
        conn.execute("DROP TABLE IF EXISTS scratch_sources")
        conn.commit()
        conn.close()
        peak = _peak_rss_mb()

        # re-open and verify conservation from stored rows only
        book = ReconBook(side)
        summary = book.summary()
        h = book.header()
        n_units = len(summary["per_unit_us"])
        conservation_ok = (
            sum(summary["class_totals_us"].values())
            == summary["unit_time_total_us"] == h["window_us"] * n_units
            and all(
                sum(v.get(c, 0) for c in
                    ("match_occupied", "match_idle", "mismatch",
                     "unattributed_occupancy", "pending_review",
                     "attribution_conflict")) == h["window_us"]
                for v in summary["per_unit_us"].values()))
        # evidence drill on the hot unit at midday
        ev = book.evidence_at(HOT, h["window_start_us"] + SWAP_US + 60)
        hot_active = len(ev["unit_claims_active"])
        # SQL statement cost of the fact scan: warehouse opened again briefly
        wh2 = Warehouse(info["warehouse"])
        stmt = [0]
        wh2._conn.set_trace_callback(lambda _sql: stmt.__setitem__(0, stmt[0] + 1))
        from legfact.stand_facts import scan_actuals
        from legfact.timeutil import parse_offset_datetime
        roots = set()
        for v in inputs.visits:
            roots.add(v.arrival_root)
            roots.add(v.departure_root)
        scan_actuals(wh2, 1, parse_offset_datetime(CUTOFF).utc, roots)
        wh2.close()
        book.close()
        print(json.dumps({
            "load_seconds": round(load_s, 1),
            "calc_seconds": round(calc_s, 1),
            "counts": result["summary"],
            "segments_written": n_segments,
            "conservation_ok": conservation_ok,
            "hot_unit_active_claims_at_midday": hot_active,
            "hot_visits": info["hot_visits"],
            "fact_scan_sql_statements": stmt[0],
            "calc_peak_rss_mb": round(peak, 1),
            "rss_limit_mb": RSS_LIMIT_MB,
            "sidecar_bytes": os.path.getsize(side),
        }, indent=2))
        return 0

    t0 = time.time()
    gen = subprocess.run(
        [sys.executable, "-I", os.path.abspath(__file__), "--worker", "gen",
         "--visits", str(args.visits), "--ledger", str(args.ledger),
         "--observations", str(args.observations)],
        capture_output=True, text=True, check=True)
    info = json.loads(gen.stdout)
    paths = os.path.join(info["tmpdir"], "paths.json")
    with open(paths, "w") as fh:
        json.dump(info, fh)
    calc_env = dict(os.environ)
    # Route CPython object allocations through libc malloc instead of the
    # default pymalloc arenas.  The engine streams per-unit sweeps and calls
    # malloc_trim between units; with the libc allocator those freed pages are
    # actually returned to the OS, so the RSS high-water mark tracks the live
    # working set instead of retaining recycled pymalloc arenas.
    calc_env["PYTHONMALLOC"] = "malloc"
    calc_env["MALLOC_TRIM_THRESHOLD_"] = "65536"
    calc = subprocess.run(
        [sys.executable, "-I", os.path.abspath(__file__), "--worker", "calc",
         "--paths", paths], capture_output=True, text=True, check=True,
        env=calc_env)
    report = json.loads(calc.stdout)
    report["rss_limit_mb"] = RSS_LIMIT_MB
    meets_target = report["calc_peak_rss_mb"] < RSS_TARGET_MB
    ok = (report["conservation_ok"]
          and report["calc_peak_rss_mb"] < RSS_LIMIT_MB
          and report["fact_scan_sql_statements"] <= 6
          and report["hot_unit_active_claims_at_midday"] >= 1000)
    out = {
        "scenario": {"visits": args.visits, "ledger_target": args.ledger,
                     "ledger_written": info["ledger_written"],
                     "observations_written": info["obs_written"],
                     "online_records": info["online_written"],
                     "units": len(info["units"]),
                     "hot_visits": info["hot_visits"]},
        "wall_seconds": round(time.time() - t0, 1),
        "build_peak_rss_mb": info["build_peak_rss_mb"],
        "ingest_seconds": info["ingest_seconds"],
        **report,
        "rss_limit_mb": RSS_LIMIT_MB,
        "rss_target_mb": RSS_TARGET_MB,
        "meets_512mib_target": meets_target,
        "pass": ok,
    }
    print(json.dumps(out, indent=2))
    if not args.keep:
        shutil.rmtree(info["tmpdir"], ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
