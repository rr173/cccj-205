#!/usr/bin/env python3
"""Independent checker for the landing-fee mart walkthrough and edge suites.

Expectations live in ``fixtures/landing/expected/*.json`` and were derived by
hand from the fixtures.  This checker only consumes the public
``LandingBook`` / ``plan`` / ``release`` API and compares the results against
those literals; it never asks the pricing code what the answer should be.

Run:  python3 -I tests/check_landing_mart.py [work_dir]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact import Warehouse  # noqa: E402
from legfact.landing import LandingBook, plan, release  # noqa: E402

FIX = os.path.abspath(os.path.join(HERE, "..", "fixtures", "landing"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("ok   " if cond else "FAIL ") + name
          + (f" :: {detail}" if detail and not cond else ""))


def _load(rel: str) -> dict:
    with open(os.path.join(FIX, rel), encoding="utf-8") as fh:
        return json.load(fh)


def _wh(path: str, suite: str, batches) -> None:
    wh = Warehouse(path)
    for b in batches:
        wh.ingest_batch(os.path.join(FIX, suite, f"manifest_{b}.json"),
                        os.path.join(FIX, suite, f"batch_{b}"))
    wh.close()


def _entry_tuples(entries):
    return sorted((e["kind"], e["root_id"], e["account"], e["month"],
                   e["amount_minor"]) for e in entries)


def run_walkthrough(work: str) -> None:
    exp = _load("expected/walkthrough_expected.json")
    wh_dir = os.path.join(work, "wh")
    _wh(wh_dir, "walkthrough", (1, 2))
    book_dir = os.path.join(work, "book")
    LandingBook.init_book(book_dir, _load("walkthrough/book.json"))

    for phase in exp["phases"]:
        r = release(
            wh_dir, book_dir, phase["receipt_batch"], phase["cutoff"],
            os.path.join(FIX, "walkthrough", phase["roster"]),
            os.path.join(FIX, "walkthrough", "tariff.json"),
            "walk-" + phase["name"].split()[0], phase["base_version"])
        tag = f"[{phase['name'].split()[0]}]"
        check(f"{tag} released a fresh version", not r["replayed"])
        v = r["version"]
        check(f"{tag} effective total",
              v["effective_total_minor"] == phase["effective_total_minor"],
              f"expected {phase['effective_total_minor']}, "
              f"got {v['effective_total_minor']}")
        check(f"{tag} entry set matches hand table",
              _entry_tuples(r["entries"]) == _entry_tuples(phase["entries"]),
              f"got {_entry_tuples(r['entries'])}")
        lines = plan(wh_dir, book_dir, phase["receipt_batch"], phase["cutoff"],
                     os.path.join(FIX, "walkthrough", phase["roster"]),
                     os.path.join(FIX, "walkthrough", "tariff.json"))["lines"]
        by_root = {ln["root_id"]: ln for ln in lines}
        for root, want in phase["priced_by_root"].items():
            got = by_root[root]
            ok = (got["status"] == "priced"
                  and got["account"] == want["account"]
                  and got["month"] == want["month"]
                  and got["serial"] == want["serial"]
                  and got["bulk_units"] == want["units"]
                  and got["tariff_entry"]["min_per_unit"] == want["rate"]
                  and got["serial_rung"]["ten_thousandths"] == want["mult"]
                  and got["amount_minor"] == want["amount_minor"])
            check(f"{tag} {root} priced as hand-computed", ok,
                  f"expected {want}, got {json.dumps(got)[:300]}")
        for root, reason in phase["unpriced"].items():
            check(f"{tag} {root} unpriced {reason}",
                  by_root[root]["status"] == "unpriced"
                  and by_root[root]["reason"] == reason)
        for root, reason in (phase["out_of_range"] or {}).items():
            check(f"{tag} {root} out of range",
                  by_root[root]["status"] == "out_of_range"
                  and by_root[root]["reason"] == reason)
        # version detail can be re-read independently
        again = plan(wh_dir, book_dir, phase["receipt_batch"], phase["cutoff"],
                     os.path.join(FIX, "walkthrough", phase["roster"]),
                     os.path.join(FIX, "walkthrough", "tariff.json"))
        check(f"{tag} summary re-adds from lines",
              sum(l["amount_minor"] for l in again["lines"]
                  if l["status"] == "priced") == v["effective_total_minor"])

    # v2 specific: net zero but six entries; reversals point at open positives
    r2 = release(wh_dir, book_dir, 2, exp["phases"][1]["cutoff"],
                 os.path.join(FIX, "walkthrough", "roster_v2.json"),
                 os.path.join(FIX, "walkthrough", "tariff.json"),
                 "walk-v2", 1)
    check("[v2] replaying the same request returns the same version",
          r2["replayed"] and r2["version"]["version_id"] == 2)
    v2_entries = r2["entries"]
    check("[v2] entry net is zero but six entries exist",
          sum(e["amount_minor"] for e in v2_entries) == 0
          and len(v2_entries) == 6)
    check("[v2] every reversal names the still-open positive it cancels",
          all(e["reverses_entry_id"] is not None
              for e in v2_entries if e["kind"] == "reversal"))
    # a dry run against the live head predicts exactly what the release
    # appends (run on its own book so the version history is unambiguous)
    fresh_wh = os.path.join(work, "wh_planonly")
    _wh(fresh_wh, "walkthrough", (1, 2))
    fresh_book = os.path.join(work, "book_planonly")
    LandingBook.init_book(fresh_book, _load("walkthrough/book.json"))
    release(fresh_wh, fresh_book, 1, exp["phases"][0]["cutoff"],
            os.path.join(FIX, "walkthrough", "roster_v1.json"),
            os.path.join(FIX, "walkthrough", "tariff.json"), "plan-a", None)
    proj = plan(fresh_wh, fresh_book, 2, exp["phases"][1]["cutoff"],
                os.path.join(FIX, "walkthrough", "roster_v2.json"),
                os.path.join(FIX, "walkthrough", "tariff.json"))[
                    "projected_changes"]
    rel = release(fresh_wh, fresh_book, 2, exp["phases"][1]["cutoff"],
                  os.path.join(FIX, "walkthrough", "roster_v2.json"),
                  os.path.join(FIX, "walkthrough", "tariff.json"), "plan-b", 1)
    got = sorted((e["kind"], e["root_id"], e["account"], e["month"],
                  e["amount_minor"]) for e in rel["entries"])
    want = sorted(
        [("reversal", e["root_id"], e["account"], e["month"],
          e["amount_minor"]) for e in proj["reversals"]]
        + [("positive", e["root_id"], e["account"], e["month"],
            e["amount_minor"]) for e in proj["positives"]])
    check("[plan] dry run predicts exactly the released entries", got == want,
          f"plan {want} vs release {got}")
    check("[plan] dry run net matches the released entry total",
          proj["net_minor"] == rel["version"]["entry_total_minor"]
          == 0)

    # ---- stability: v2 cannot be replayed once v3 has superseded it
    try:
        release(wh_dir, book_dir, 2, exp["phases"][1]["cutoff"],
                os.path.join(FIX, "walkthrough", "roster_v3.json"),
                os.path.join(FIX, "walkthrough", "tariff.json"),
                "walk-v2-again", 1)
        check("[v3] a new request against a superseded base is rejected", False)
    except Exception as exc:  # ReleaseConflict
        check("[v3] a new request against a superseded base is rejected",
              getattr(exc, "code", "") == "stale_base_version")
    # ---- v1 detail is byte-identical after v2 and v3 published
    book = LandingBook(book_dir)
    v1_lines = book.lines(1)
    check("[v1] published detail never changes when later versions land",
          sum(l["amount_minor"] for l in v1_lines if l["status"] == "priced")
          == 2500
          and [l["root_id"] for l in v1_lines] == ["R-A", "R-B", "R-C"])
    # ---- removing a root from the roster is not an error: it is reversed
    tail_roster = _load("walkthrough/roster_v3.json")
    tail_roster["roots"] = [r for r in tail_roster["roots"]
                            if r["root_id"] != "R-B"]
    tail_path = os.path.join(work, "roster_no_b.json")
    with open(tail_path, "w") as fh:
        json.dump(tail_roster, fh)
    r4 = release(wh_dir, book_dir, 2, exp["phases"][2]["cutoff"], tail_path,
                 os.path.join(FIX, "walkthrough", "tariff.json"), "walk-v4", 3)
    kinds = {(e["kind"], e["root_id"]) for e in r4["entries"]}
    check("[v4] removing a root reverses it and re-serialises the rest",
          ("reversal", "R-B") in kinds
          and r4["version"]["effective_total_minor"] == 2000
          and all(e["amount_minor"] == -1000 for e in r4["entries"]
                  if e["kind"] == "reversal"))
    det = {l["root_id"]: l for l in book.lines(r4["version"]["version_id"])}
    check("[v4] month of an unchanged landing is retained, not recomputed",
          det["R-A"]["month"] == "2026-02" and det["R-A"]["serial"] == 1)
    # v4 detail must contain only the remaining roster rows
    check("[v4] removed root is absent from the version detail",
          "R-B" not in det)
    book.close()


def run_edge(work: str) -> None:
    exp = _load("expected/edge_expected.json")
    wh_dir = os.path.join(work, "wh_edge")
    _wh(wh_dir, "edge", (1,))
    book_dir = os.path.join(work, "book_edge")
    LandingBook.init_book(book_dir, _load("edge/book.json"))
    p = plan(wh_dir, book_dir, exp["receipt_batch"], exp["cutoff"],
             os.path.join(FIX, "edge", "roster.json"),
             os.path.join(FIX, "edge", "tariff.json"))
    by_root = {ln["root_id"]: ln for ln in p["lines"]}
    for root, want in exp["priced_by_root"].items():
        got = by_root[root]
        ok = (got["status"] == "priced"
              and got["account"] == want["account"]
              and got["month"] == want["month"]
              and got["serial"] == want["serial"]
              and got["bulk_units"] == want["units"]
              and got["tariff_entry"]["min_per_unit"] == want["rate"]
              and got["serial_rung"]["ten_thousandths"] == want["mult"]
              and got["amount_minor"] == want["amount_minor"])
        check(f"[edge] {root} hand-computed", ok,
              f"expected {want}, got {json.dumps(got)[:300]}")
    for root, reason in exp["unpriced"].items():
        check(f"[edge] {root} unpriced {reason}",
              by_root[root]["status"] == "unpriced"
              and by_root[root]["reason"] == reason,
              json.dumps(by_root[root])[:200])
    for root, reason in exp["out_of_range"].items():
        check(f"[edge] {root} visible but not charged",
              by_root[root]["status"] == "out_of_range"
              and by_root[root]["reason"] == reason)
    check("[edge] total",
          p["summary"]["total_minor"] == exp["effective_total_minor"],
          f"expected {exp['effective_total_minor']}, "
          f"got {p['summary']['total_minor']}")
    check("[edge] zero-amount line is not an unpriced line",
          by_root["R-Z"]["status"] == "priced"
          and by_root["R-Z"]["amount_minor"] == 0)
    # the multi-report landing and the renamed landing each appear once
    check("[edge] three reports of one landing -> one line",
          sum(1 for l in p["lines"] if l["root_id"] == "R-MULTI") == 1)
    check("[edge] rename does not create a second landing",
          sum(1 for l in p["lines"] if l["root_id"].startswith("R-REN")) == 1)
    # release once: zero amount must still carry a posting identity
    r = release(wh_dir, book_dir, exp["receipt_batch"], exp["cutoff"],
                os.path.join(FIX, "edge", "roster.json"),
                os.path.join(FIX, "edge", "tariff.json"), "edge-v1", None)
    detail = {l["root_id"]: l for l in
              LandingBook(book_dir).lines(r["version"]["version_id"])}
    check("[edge] zero-amount priced line keeps its posting",
          detail["R-Z"]["posting_id"] is not None
          and detail["R-Z"]["amount_minor"] == 0)
    check("[edge] unpriced lines carry no posting",
          detail["R-D"]["posting_id"] is None
          and detail["R-O"]["posting_id"] is None)
    # re-release with the same request is a no-op
    r2 = release(wh_dir, book_dir, exp["receipt_batch"], exp["cutoff"],
                 os.path.join(FIX, "edge", "roster.json"),
                 os.path.join(FIX, "edge", "tariff.json"), "edge-v1", None)
    check("[edge] same request id replays the original version",
          r2["replayed"])


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(
        prefix="landing_check_")
    os.makedirs(base, exist_ok=True)
    run_walkthrough(base)
    run_edge(base)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
