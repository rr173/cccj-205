#!/usr/bin/env python3
"""Runtime-generated scale rehearsal for the landing-fee data mart.

Nothing here is committed as data: the script synthesizes an airport book with
``--roots`` operating roots and ``--messages`` sparse AIDX reports into a
temporary directory, runs one whole-book plan + release, then a correction
batch that moves a slice of landings across the February/March partition and
releases again.

Reported for each phase:

* roots inside / outside the pricing scope and why,
* priced vs unresolved line counts,
* partition balances per (account, month),
* process peak RSS (must stay under 384 MiB),
* SQL statement count of the calculation (proves the scan is one pass over
  the knowledge cut, not one re-scan per roster row).

Run:  python3 -I tests/scale_landing.py [--roots 30000] [--messages 120000]
                                          [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generate_fixtures import (aidx_xml, identity_file, rel_initial,  # noqa: E402
                               write_batch)
from legfact import Warehouse  # noqa: E402
from legfact.landing import LandingBook, plan, release, verify  # noqa: E402
from legfact.landing_config import BookConfig  # noqa: E402

AUTH = "SHA-OCC"
T0 = "2026-01-01T00:00:00+08:00"
ACCOUNTS = ("ACCT-A", "ACCT-B", "ACCT-C")


def _key(i: int) -> dict:
    return {"carrier": "CA", "flight_no": str(100000 + i), "suffix": "",
            "service_date": "2026-01-01", "departure": "PEK", "arrival": "SHA"}


def _xml(event_id, i, actual, *, revision=1, effective_at):
    return aidx_xml(
        event_id=event_id, sender=AUTH, revision=revision,
        effective_at=effective_at, identity_at=T0,
        flight_no=str(100000 + i),
        service_date="2026-01-01", dep="PEK", arr="SHA",
        times={"act_arr": actual} if actual else {
            "sched_arr": "2026-01-05T10:00:00+08:00",
            "est_arr": "2026-01-05T10:10:00+08:00"})


def build_book(td: str, roots: int, messages: int) -> tuple[str, str, str, str]:
    """Create warehouse batches, roster, tariff and book config."""
    wh_dir = os.path.join(td, "wh")
    roster = {"airport": "SHA", "roots": []}
    rels = []
    # Root mix: 60% January, 20% February, 10% March, 5% April (out of book),
    # 5% no actual at all.  A few roster roots are unknown or wrong-airport.
    jan = int(roots * 0.60)
    feb = int(roots * 0.20)
    mar = int(roots * 0.10)
    apr = int(roots * 0.05)
    for i in range(roots):
        k = _key(i)
        rels.append(rel_initial(f"IL-{i}", f"R{i}", k, T0, AUTH, f"J-{i}"))
        roster["roots"].append({
            "root_id": f"R{i}", "root": k,
            "account": ACCOUNTS[i % len(ACCOUNTS)],
            "weight_kg": 1000 + (i % 7) * 100,
            "category": ("JET", "TURBO", "PROP")[i % 3]})
    # 200 roster entries with an operating key no initial_leg declares,
    # 100 whose initial key arrives elsewhere -> unpriced scope rows.
    for j in range(200):
        k = dict(_key(roots + j), flight_no=str(900000 + j))
        roster["roots"].append({
            "root_id": f"R-UNK-{j}", "root": k,
            "account": ACCOUNTS[j % len(ACCOUNTS)], "weight_kg": 1000,
            "category": "JET"})
    for j in range(100):
        k = dict(_key(roots + 500 + j), arrival="PEK")
        rels.append(rel_initial(f"IL-FAR-{j}", f"R-FAR-{j}", k, T0, AUTH,
                                f"J-FAR-{j}"))
        roster["roots"].append({
            "root_id": f"R-FAR-{j}", "root": k,
            "account": ACCOUNTS[j % len(ACCOUNTS)], "weight_kg": 1000,
            "category": "JET"})

    id_doc = identity_file(rels)

    def actual_for(i: int) -> str | None:
        if i < jan:
            day = 1 + (i % 28)
            return f"2026-01-{day:02d}T{(6 + i % 12):02d}:{(i * 7) % 60:02d}:00+08:00"
        if i < jan + feb:
            day = 1 + (i % 27)
            return f"2026-02-{day:02d}T{(6 + i % 12):02d}:{(i * 7) % 60:02d}:00+08:00"
        if i < jan + feb + mar:
            day = 1 + (i % 27)
            return f"2026-03-{day:02d}T{(6 + i % 12):02d}:{(i * 7) % 60:02d}:00+08:00"
        if i < jan + feb + mar + apr:
            return f"2026-04-0{1 + i % 9}T12:00:00+08:00"
        return None

    # Spread the sparse reports over 6 handover batches.  Every root with an
    # actual gets at least one report; most get a duplicate retransmission of
    # the same event id (sparse and idempotent), a few get a later revision.
    files_by_batch: dict[int, dict] = {b: {} for b in range(1, 7)}
    files_by_batch[1]["identity_b1.json"] = (id_doc, "identity_json")
    msg_seq = 0
    for i in range(roots):
        actual = actual_for(i)
        if actual is None:
            # still report scheduled facts so the root is "known but unlanded"
            b = 1 + (i % 6)
            files_by_batch[b][f"m_{i}_none.xml"] = (
                _xml(f"E-{i}-none", i, None,
                     effective_at=f"2026-01-02T0{1 + i % 9}:00:00+08:00"),
                "aidx_xml")
            msg_seq += 1
            continue
        reports = 2 if i % 3 else 3
        for r in range(reports):
            b = 1 + ((i + r) % 6)
            eff = actual  # all reports are visible at the cutoff
            files_by_batch[b][f"m_{i}_{r}.xml"] = (
                _xml(f"E-{i}-{r}", i, actual, revision=r + 1,
                     effective_at=eff), "aidx_xml")
            msg_seq += 1
    # top up / trim to the requested message count with extra retransmissions
    pad = 0
    while msg_seq < messages:
        # only roots that actually have an actual arrival are re-reported
        i = pad % (jan + feb + mar + apr)
        b = 1 + (pad % 6)
        files_by_batch[b][f"pad_{pad}.xml"] = (
            _xml(f"E-{i}-pad{pad}", i, actual_for(i) or
                 "2026-01-05T10:00:00+08:00", revision=99,
                 effective_at="2026-01-03T00:00:00+08:00"), "aidx_xml")
        msg_seq += 1
        pad += 1

    for b in range(1, 7):
        write_batch(os.path.join(td, "batches"), b, AUTH, files_by_batch[b])

    tariff = {"airport": "SHA", "currency": "CNY", "rates": [
        {"category": "JET", "valid_from": "2026-01-01",
         "valid_to": "2026-02-01", "min_per_unit": 1000},
        {"category": "JET", "valid_from": "2026-02-01",
         "valid_to": "2026-04-01", "min_per_unit": 1100},
        {"category": "TURBO", "valid_from": "2026-01-01",
         "valid_to": "2026-04-01", "min_per_unit": 700},
        {"category": "PROP", "valid_from": "2026-01-01",
         "valid_to": "2026-04-01", "min_per_unit": 400}],
        "serials": [{"account": a,
                     "rungs": [{"upto_serial": 1, "ten_thousandths": 10000},
                               {"upto_serial": 200, "ten_thousandths": 9000},
                               {"upto_serial": None, "ten_thousandths": 8000}]}
                    for a in ACCOUNTS]}
    roster_path = os.path.join(td, "roster.json")
    tariff_path = os.path.join(td, "tariff.json")
    for path, doc in ((roster_path, roster), (tariff_path, tariff)):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, sort_keys=True)
    return wh_dir, roster_path, tariff_path, os.path.join(td, "book")


def build_correction(td: str, roots: int, jan: int) -> int:
    """Batch 7: move 1500 February landings across the month boundary."""
    files = {}
    start = jan + 0  # first February root
    for n in range(1500):
        i = start + n
        files[f"corr_{n}.xml"] = (
            _xml(f"E-{i}-corr", i, "2026-03-01T00:10:00+08:00", revision=50,
                 effective_at="2026-03-02T00:00:00+08:00"), "aidx_xml")
    write_batch(os.path.join(td, "batches"), 7, AUTH, files)
    return 7


def _peak_rss_mb() -> float:
    """Peak resident set of THIS process (kernel high-water mark)."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _build_worker(roots: int, messages: int) -> dict:
    """Child process 1: synthesize fixtures and ingest every batch."""
    td = tempfile.mkdtemp(prefix="landing_scale_")
    wh_dir, roster_path, tariff_path, book_dir = build_book(
        td, roots, messages)
    build_correction(td, roots, int(roots * 0.60))
    wh = Warehouse(wh_dir)
    reports = []
    for b in range(1, 8):
        rep = wh.ingest_batch(
            os.path.join(td, "batches", f"manifest_{b}.json"),
            os.path.join(td, "batches", f"batch_{b}"))
        reports.append({"batch": b, "accepted": rep.events_accepted,
                        "retransmitted": rep.events_retransmitted})
    wh.close()
    LandingBook.init_book(book_dir, {
        "airport": "SHA", "timezone": "Asia/Shanghai",
        "months": ["2026-01", "2026-02", "2026-03"], "currency": "CNY"})
    return {"tmpdir": td, "wh_dir": wh_dir, "roster": roster_path,
            "tariff": tariff_path, "book": book_dir, "ingest_batches": reports,
            "build_peak_rss_mb": round(_peak_rss_mb(), 1)}


def _calc_worker(paths: dict) -> dict:
    """Child process 2: the calculation job whose RSS we measure."""
    wh_dir = paths["wh_dir"]
    book_dir = paths["book"]
    roster_path = paths["roster"]
    tariff_path = paths["tariff"]
    t = time.time()
    p = plan(wh_dir, book_dir, 6, "2026-05-01T00:00:00+08:00",
             roster_path, tariff_path)
    plan_s = time.time() - t
    scan_stats = p["summary"]["scan_stats"]
    t = time.time()
    r1 = release(wh_dir, book_dir, 6, "2026-05-01T00:00:00+08:00",
                 roster_path, tariff_path, "scale-v1", None)
    release_s = time.time() - t
    peak_v1 = _peak_rss_mb()
    t = time.time()
    r2 = release(wh_dir, book_dir, 7, "2026-05-01T00:00:00+08:00",
                 roster_path, tariff_path, "scale-v2",
                 r1["version"]["version_id"])
    release2_s = time.time() - t
    peak_v2 = _peak_rss_mb()
    ver = verify(book_dir)
    return {
        "plan_seconds": round(plan_s, 2),
        "plan_scan": scan_stats,
        "plan_counts": {
            "roster": p["summary"]["roster_count"],
            "priced": p["summary"]["priced_count"],
            "unpriced": p["summary"]["unpriced_count"],
            "out_of_range": p["summary"]["out_of_range_count"],
            "unpriced_by_reason": p["summary"]["unpriced_by_reason"]},
        "v1": {
            "release_seconds": round(release_s, 2),
            "priced": r1["version"]["priced_count"],
            "unresolved": r1["version"]["unpriced_count"]
                         + r1["version"]["out_of_range_count"],
            "entries": r1["version"]["entry_count"],
            "effective_total_minor": r1["version"]["effective_total_minor"],
            "balances": r1["effective"]["balances"]},
        "v2_cross_partition": {
            "release_seconds": round(release2_s, 2),
            "priced": r2["version"]["priced_count"],
            "entries": r2["version"]["entry_count"],
            "entry_net_minor": r2["version"]["entry_total_minor"],
            "reversals": sum(1 for e in r2["entries"]
                             if e["kind"] == "reversal"),
            "positives": sum(1 for e in r2["entries"]
                             if e["kind"] == "positive"),
            "balances": r2["effective"]["balances"]},
        "ledger_verify": {"ok": ver["ok"], "problems": ver["problems"]},
        "calc_peak_rss_mb": round(peak_v2, 1),
        "peak_after_plan_mb": round(peak_v1, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", type=int, default=30000)
    ap.add_argument("--messages", type=int, default=120000)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--worker", choices=("build", "calc"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--paths", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker == "build":
        print(json.dumps(_build_worker(args.roots, args.messages)))
        return 0
    if args.worker == "calc":
        with open(args.paths) as fh:
            print(json.dumps(_calc_worker(json.load(fh))))
        return 0

    t0 = time.time()
    build = subprocess.run(
        [sys.executable, "-I", os.path.abspath(__file__), "--worker", "build",
         "--roots", str(args.roots), "--messages", str(args.messages)],
        capture_output=True, text=True, check=True)
    built = json.loads(build.stdout)
    paths_file = os.path.join(built["tmpdir"], "paths.json")
    with open(paths_file, "w") as fh:
        json.dump(built, fh)
    try:
        calc = subprocess.run(
            [sys.executable, "-I", os.path.abspath(__file__), "--worker", "calc",
             "--paths", paths_file],
            capture_output=True, text=True, check=True)
        c = json.loads(calc.stdout)
        out = {
            "scenario": {"roots": args.roots, "messages": args.messages,
                         "correction_roots": 1500, "batches": 7,
                         "isolation": "fixture build + ingest and the "
                                      "calculation job run in separate "
                                      "processes so the reported peak is the "
                                      "calculation job's own"},
            "wall_seconds": round(time.time() - t0, 1),
            "build_peak_rss_mb": built["build_peak_rss_mb"],
            "ingest_batches": built["ingest_batches"],
            **c,
            "rss_limit_mb": 384,
        }
        ok = (c["calc_peak_rss_mb"] < 384 and c["ledger_verify"]["ok"]
              and c["plan_scan"]["sql_statements"] <= 12
              and c["v2_cross_partition"]["reversals"] > 0
              and c["v2_cross_partition"]["positives"] > 0)
        out["pass"] = ok
        print(json.dumps(out, indent=2))
        return 0 if ok else 1
    finally:
        if args.keep:
            print(json.dumps({"kept": built["tmpdir"]}), file=sys.stderr)
        else:
            shutil.rmtree(built["tmpdir"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
