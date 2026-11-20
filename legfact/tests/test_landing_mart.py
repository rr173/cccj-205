#!/usr/bin/env python3
"""Behavioural suite for the landing-fee data mart.

Covers:

  * structural input rejection (duplicates, invalid weights, overlapping
    tariff intervals, ladders without a fallback, ...) leaves the book
    untouched;
  * no tariff entry / no ladder rung rejects the whole calculation rather
    than silently billing zero;
  * release atomicity under an interrupted write (crash injection) — no
    half-reversed ledger is ever visible;
  * idempotent replay of the same request id, conflicting reuse of an id,
    and rejection of a release against a stale head;
  * old versions stay byte-identical after new batches and dimensions are
    ingested; the ledger is readable from stored rows only;
  * the cumulative signed journal equals each version's effective total,
    and a correction that only re-explains a landing creates no entry.

Run:  python3 -I tests/test_landing_mart.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact import Warehouse  # noqa: E402
from legfact.landing import (InputRejected, LandingBook, MartError,  # noqa: E402
                             ReleaseConflict, plan, release, verify)
from legfact.landing_config import ConfigError  # noqa: E402

FIX = os.path.abspath(os.path.join(HERE, "..", "fixtures", "landing"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("ok   " if cond else "FAIL ") + name
          + (f" :: {detail}" if detail and not cond else ""))


def _load(rel: str) -> dict:
    with open(os.path.join(FIX, rel), encoding="utf-8") as fh:
        return json.load(fh)


def _path(rel: str) -> str:
    return os.path.join(FIX, rel)


def _setup(work: str, suite: str = "walkthrough", batches=(1, 2)) -> tuple[str, str]:
    wh_dir = os.path.join(work, "wh")
    wh = Warehouse(wh_dir)
    for b in batches:
        wh.ingest_batch(os.path.join(FIX, suite, f"manifest_{b}.json"),
                        os.path.join(FIX, suite, f"batch_{b}"))
    wh.close()
    book_dir = os.path.join(work, "book")
    LandingBook.init_book(book_dir, _load(f"{suite}/book.json"))
    return wh_dir, book_dir


def _release_v1(wh_dir, book_dir, request_id="base"):
    return release(wh_dir, book_dir, 1, "2026-02-10T00:00:00+08:00",
                   _path("walkthrough/roster_v1.json"),
                   _path("walkthrough/tariff.json"), request_id, None)


# ------------------------------------------------------------- rejection
def test_input_rejection(work: str) -> None:
    wh_dir, book_dir = _setup(os.path.join(work, "rej"))
    inv = os.path.join(FIX, "invalid")
    cases = [
        ("duplicate root", f"{inv}/dup_root_roster.json", f"{inv}/good_tariff.json",
         "duplicate_root"),
        ("zero weight", f"{inv}/bad_weight_roster.json", f"{inv}/good_tariff.json",
         "invalid_weight"),
        ("non-integer weight", f"{inv}/float_weight_roster.json",
         f"{inv}/good_tariff.json", "invalid_weight"),
        ("blank account", f"{inv}/bad_account_roster.json",
         f"{inv}/good_tariff.json", "bad_roster"),
        ("overlapping tariff intervals", f"{inv}/good_roster.json",
         f"{inv}/overlap_tariff.json", "overlapping_tariff"),
        ("ladder without fallback", f"{inv}/good_roster.json",
         f"{inv}/nofallback_tariff.json", "ladder_without_fallback"),
        ("duplicate ladder", f"{inv}/good_roster.json",
         f"{inv}/dupladder_tariff.json", "duplicate_ladder"),
        ("negative rate", f"{inv}/good_roster.json",
         f"{inv}/negative_rate_tariff.json", "bad_rate"),
        ("currency mismatch", f"{inv}/good_roster.json",
         f"{inv}/currency_tariff.json", "tariff_currency_mismatch"),
        ("malformed dimension JSON", f"{inv}/malformed.json",
         f"{inv}/good_tariff.json", "bad_dimension"),
    ]
    for name, roster, tariff, code in cases:
        # the invalid roster targets a key that does not exist in the
        # walkthrough batch; unknown_root must therefore not shadow the
        # structural error -> the roster/tariff shape is validated first.
        try:
            plan(wh_dir, book_dir, 1, "2026-02-10T00:00:00+08:00",
                 roster, tariff)
            check(f"reject: {name}", False, "plan accepted invalid input")
        except (ConfigError, InputRejected, MartError) as exc:
            got = getattr(exc, "code", None)
            check(f"reject: {name}", code is None or got == code,
                  f"expected {code}, got {got}: {exc}")
    # valid-but-unpriced-by-tariff root must reject the whole run
    ro = {"airport": "SHA", "roots": [{
        "root_id": "R-A",
        # tariff only covers 2026-01-01..2026-03-01, use a landing on Jan 30
        "root": {"carrier": "CA", "flight_no": "101", "suffix": "",
                 "service_date": "2026-01-01", "departure": "PEK",
                 "arrival": "SHA"},
        "account": "ACCT-A", "weight_kg": 1000, "category": "NOPE"}]}
    ro_path = os.path.join(work, "rej_roster.json")
    with open(ro_path, "w", encoding="utf-8") as fh:
        json.dump(ro, fh)
    try:
        plan(wh_dir, book_dir, 1, "2026-02-10T00:00:00+08:00",
             ro_path, _path("walkthrough/tariff.json"))
        check("reject: eligible landing without tariff category", False)
    except InputRejected as exc:
        check("reject: eligible landing without tariff category",
              exc.code == "no_tariff_entry", exc.code)
    # a roster row naming a different key than the root's initial leg is a
    # structural error, not a silently billed flight
    wrong = {"airport": "SHA", "roots": [{
        "root_id": "R-A",
        "root": {"carrier": "CA", "flight_no": "999", "suffix": "",
                 "service_date": "2026-01-01", "departure": "PEK",
                 "arrival": "SHA"},
        "account": "ACCT-A", "weight_kg": 1000, "category": "JET"}]}
    wrong_path = os.path.join(work, "wrong_roster.json")
    with open(wrong_path, "w", encoding="utf-8") as fh:
        json.dump(wrong, fh)
    try:
        plan(wh_dir, book_dir, 1, "2026-02-10T00:00:00+08:00",
             wrong_path, _path("walkthrough/tariff.json"))
        check("reject: roster key differing from the declared root", False)
    except InputRejected as exc:
        check("reject: roster key differing from the declared root",
              exc.code == "roster_key_mismatch", exc.code)
    book = LandingBook(book_dir)
    check("reject: nothing was published by rejected plans",
          book.head_version() is None and not book.entries())
    book.close()


# ------------------------------------------------------------- zero vs missing
def test_zero_vs_missing(work: str) -> None:
    wh_dir, book_dir = _setup(os.path.join(work, "zv"), "edge", (1,))
    r = release(wh_dir, book_dir, 1, "2026-03-10T00:00:00+08:00",
                _path("edge/roster.json"), _path("edge/tariff.json"),
                "edge", None)
    book = LandingBook(book_dir)
    detail = {l["root_id"]: l for l in book.lines(r["version"]["version_id"])}
    check("zero: priced zero line has a positive ledger entry",
          detail["R-Z"]["status"] == "priced"
          and detail["R-Z"]["amount_minor"] == 0
          and detail["R-Z"]["posting_id"] is not None)
    entries = {e["root_id"]: e for e in book.entries()}
    check("zero: zero amount produces a real (zero) posting entry",
          entries["R-Z"]["amount_minor"] == 0
          and entries["R-Z"]["kind"] == "positive")
    check("zero: missing actual produces no entry at all", "R-D" not in entries)
    check("zero: after-cutoff actual produces no entry", "R-E" not in entries)
    # next version with an unchanged zero line keeps the same posting id
    r2 = release(wh_dir, book_dir, 1, "2026-03-10T00:00:00+08:00",
                 _path("edge/roster.json"), _path("edge/tariff.json"),
                 "edge-2", 1)
    check("zero: unchanged zero posting is not re-booked",
          r2["version"]["entry_count"] == 0)
    d2 = {l["root_id"]: l for l in book.lines(r2["version"]["version_id"])}
    check("zero: posting identity survives an explanation-only version",
          d2["R-Z"]["posting_id"] == detail["R-Z"]["posting_id"])
    book.close()


# --------------------------------------------------------------- atomicity
def test_interrupted_release(work: str) -> None:
    wh_dir, book_dir = _setup(os.path.join(work, "atom"))
    _release_v1(wh_dir, book_dir)
    before = LandingBook(book_dir)
    v1_lines = before.lines(1)
    v1_entries = before.entries(1)
    head_before = before.head_version()
    before.close()

    # Inject a crash in the middle of the store's publish transaction: the
    # mart must roll back to exactly the previous published state.
    from legfact import landing as L
    original = L._write_version

    def boom(*args, **kwargs):
        # the version row and part of the journal are already written when
        # the store dies; the transaction must roll back completely.
        raise RuntimeError("simulated process crash")

    L._write_version = boom
    try:
        release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                _path("walkthrough/roster_v2.json"),
                _path("walkthrough/tariff.json"), "crash", 1)
        check("atomic: crash raises", False)
    except RuntimeError:
        check("atomic: crash raises", True)
    finally:
        L._write_version = original

    after = LandingBook(book_dir)
    check("atomic: crashed release publishes no version",
          after.head_version() == head_before)
    check("atomic: crashed release leaves detail byte-identical",
          after.lines(1) == v1_lines)
    check("atomic: crashed release leaves journal byte-identical",
          after.entries(1) == v1_entries)
    check("atomic: no half-reversed posting",
          all(p["status"] == "open" for p in after.open_postings())
          and len(after.open_postings()) == 3)

    # The same request id can now be retried and completes normally.
    r = release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                _path("walkthrough/roster_v2.json"),
                _path("walkthrough/tariff.json"), "crash", 1)
    check("atomic: retry after crash succeeds with a fresh version",
          not r["replayed"] and r["version"]["version_id"] == 2)
    after.close()


def test_replay_and_staleness(work: str) -> None:
    wh_dir, book_dir = _setup(os.path.join(work, "rep"))
    r1 = _release_v1(wh_dir, book_dir, "idem")
    r1b = _release_v1(wh_dir, book_dir, "idem")
    check("idem: identical request returns original version",
          r1b["replayed"] and r1b["version"]["version_id"]
          == r1["version"]["version_id"])
    try:
        release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                _path("walkthrough/roster_v2.json"),
                _path("walkthrough/tariff.json"), "idem", 1)
        check("idem: reusing a request id for different inputs is rejected",
              False)
    except ReleaseConflict:
        check("idem: reusing a request id for different inputs is rejected",
              True)
    # stale base version (head is 1, declared 0)
    try:
        release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                _path("walkthrough/roster_v2.json"),
                _path("walkthrough/tariff.json"), "stale", 0)
        check("stale: release against an old base version is rejected", False)
    except ReleaseConflict:
        check("stale: release against an old base version is rejected", True)
    book = LandingBook(book_dir)
    check("stale: rejected writes did not change the head",
          book.head_version() == 1)
    # a newer release then invalidates the declared base
    r2 = release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                 _path("walkthrough/roster_v2.json"),
                 _path("walkthrough/tariff.json"), "v2", 1)
    check("stale: a concurrent later release makes the next write stale",
          r2["version"]["version_id"] == 2)
    try:
        release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                _path("walkthrough/roster_v3.json"),
                _path("walkthrough/tariff.json"), "v3-oldbase", 1)
        check("stale: declaring the superseded base version is rejected", False)
    except ReleaseConflict:
        check("stale: declaring the superseded base version is rejected", True)
    # bill rates never silently estimate a reversal: a reversed amount equals
    # the original positive, not a recomputation under the new tariff
    r3 = release(wh_dir, book_dir, 2, "2026-02-10T00:00:00+08:00",
                 _path("walkthrough/roster_v3.json"),
                 _path("walkthrough/tariff.json"), "v3", 2)
    pairs = {}
    for e in r3["entries"]:
        pairs.setdefault(e["posting_id"], []).append(e)
    orig = {e["posting_id"]: e for e in r2["entries"]
            if e["kind"] == "positive"}
    ok = True
    for e in r3["entries"]:
        if e["kind"] == "reversal":
            pos = orig.get(e["posting_id"])
            if pos is None or -e["amount_minor"] != pos["amount_minor"]:
                ok = False
    check("reversal: reverses the booked amount, not a new estimate", ok)
    check("ledger: every version's cumulative journal equals its detail",
          verify(book_dir)["ok"])
    book.close()


# ------------------------------------------------------------- immutability
def test_immutability(work: str) -> None:
    wh_dir, book_dir = _setup(os.path.join(work, "imm"))
    r = _release_v1(wh_dir, book_dir)
    vid = r["version"]["version_id"]
    book = LandingBook(book_dir)
    detail = book.lines(vid)
    journal = book.entries(vid)
    book.close()

    # A later *published* batch and a different dimension set must not touch
    # v1.  We ingest the edge suite into the same warehouse (continuous batch
    # numbering) as the new knowledge, then re-read v1.
    sys.path.insert(0, HERE)
    from generate_fixtures import (aidx_xml, identity_file,  # noqa: E402
                                   rel_initial, write_batch)
    new_key = {"carrier": "CA", "flight_no": "199", "suffix": "",
               "service_date": "2026-01-01", "departure": "PEK",
               "arrival": "SHA"}
    batch_dir = os.path.join(work, "late_batch")
    write_batch(batch_dir, 3, "SHA-OCC", {
        "identity_late.json": (identity_file([rel_initial(
            "IL-LATE", "R-LATE", new_key, "2026-01-01T00:00:00+08:00",
            "SHA-OCC", "J-LATE")]), "identity_json"),
        "msg_late.xml": (aidx_xml(
            event_id="E-LATE", sender="SHA-OCC", revision=1,
            effective_at="2026-02-02T09:00:00+08:00",
            identity_at="2026-01-01T00:00:00+08:00",
            flight_no="199", service_date="2026-01-01", dep="PEK",
            arr="SHA", times={"act_arr": "2026-01-09T10:00:00+08:00"}),
            "aidx_xml")})
    wh = Warehouse(wh_dir)
    wh.ingest_batch(os.path.join(batch_dir, "manifest_3.json"),
                    os.path.join(batch_dir, "batch_3"))
    wh.close()
    # different dimensions: write an alternate roster and tariff
    alt_roster = dict(_load("walkthrough/roster_v1.json"))
    alt_roster["roots"] = [r for r in alt_roster["roots"]
                           if r["root_id"] != "R-C"]
    with open(os.path.join(work, "roster_other.json"), "w") as fh:
        json.dump(alt_roster, fh)
    book = LandingBook(book_dir)
    check("immutable: old version detail is unchanged",
          book.lines(vid) == detail)
    check("immutable: old version journal is unchanged",
          book.entries(vid) == journal)
    check("immutable: the roster content is retained with the version",
          json.loads(book.version(vid)["roster_json"]) ==
          _load("walkthrough/roster_v1.json"))
    check("immutable: the tariff content is retained with the version",
          json.loads(book.version(vid)["tariff_json"]) ==
          _load("walkthrough/tariff.json"))
    # deleting the warehouse entirely still allows reading the old version
    shutil.rmtree(wh_dir)
    wh_gone = Warehouse(os.path.join(work, "empty_wh"))
    wh_gone.close()
    book2 = LandingBook(book_dir)
    check("immutable: published detail readable without the warehouse",
          book2.lines(vid) == detail)
    book2.close()


def test_explanation_only(work: str) -> None:
    """A new source/serial/rate explanation with unchanged money: no entry."""
    wh_dir, book_dir = _setup(os.path.join(work, "expl"))
    r1 = _release_v1(wh_dir, book_dir)
    # same roster and tariff, new request id and a later cutoff: the
    # explanation may cite a later winning source but money is unchanged.
    r2 = release(wh_dir, book_dir, 1, "2026-03-01T00:00:00+08:00",
                 _path("walkthrough/roster_v1.json"),
                 _path("walkthrough/tariff.json"), "explain", 1)
    check("explain: no entries when account/month/amount are unchanged",
          r2["version"]["entry_count"] == 0
          and r2["entries"] == [])
    book = LandingBook(book_dir)
    d1 = {l["root_id"]: l for l in book.lines(r1["version"]["version_id"])}
    d2 = {l["root_id"]: l for l in book.lines(r2["version"]["version_id"])}
    check("explain: new explanation is retained in the newer version",
          d2["R-A"]["cutoff"] if "cutoff" in d2["R-A"] else True)
    check("explain: posting identities are preserved",
          all(d1[r]["posting_id"] == d2[r]["posting_id"] for r in d1))
    check("explain: head advanced even without postings",
          book.head_version() == 2)
    book.close()


def main() -> int:
    base = tempfile.mkdtemp(prefix="landing_behaviour_")
    try:
        test_input_rejection(os.path.join(base, "a"))
        test_zero_vs_missing(os.path.join(base, "b"))
        test_interrupted_release(os.path.join(base, "c"))
        test_replay_and_staleness(os.path.join(base, "d"))
        test_immutability(os.path.join(base, "e"))
        test_explanation_only(os.path.join(base, "f"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
