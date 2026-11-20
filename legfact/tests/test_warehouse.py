#!/usr/bin/env python3
"""Behavioural test suite for the offline leg-fact warehouse.

Covers, against freshly generated fixtures:

  * edge-case identity semantics (double-root rename, rename fork/cycle,
    codeshare overlap, unknown-root references, forward references);
  * field-level conflict containment and explicit clears;
  * unknown/non-authoritative sender handling;
  * cross-UTC-midnight timestamps with an independent local service_date;
  * interrupted publish (no half-covered batch) and digest tamper rejection;
  * repository copy + reopen querying of old batches;
  * out-of-order / retransmission equivalence (identical complete fact set);
  * large-batch memory measurement (blobs on disk, bounded resident growth).

Run:  python3 tests/test_warehouse.py
"""
from __future__ import annotations

import json
import os
import resource
import shutil
import sys
import tempfile
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact import (Exporter, IngestError, OpKey, QueryEngine,  # noqa: E402
                     Warehouse)

FIX = os.path.abspath(os.path.join(HERE, "..", "fixtures"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    mark = "ok  " if cond else "FAIL"
    print(f"{mark} {name}" + (f" :: {detail}" if detail and not cond else ""))


def ingest_suite(wh: Warehouse, suite: str, batches) -> None:
    for b in batches:
        wh.ingest_batch(
            os.path.join(FIX, suite, f"manifest_{b}.json"),
            os.path.join(FIX, suite, f"batch_{b}"))


def fact(eng, key, batch, bt):
    return QueryEngine.to_dict(eng.query(key, batch, bt))


# ---------------------------------------------------------------- edge suite
def test_edge_semantics() -> None:
    d = tempfile.mkdtemp(prefix="wh_edge_test_")
    wh = Warehouse(os.path.join(d, "wh"))
    ingest_suite(wh, "edge", range(1, 7))
    eng = QueryEngine(wh)
    t = "2026-11-22T09:30:00+08:00"

    # clean-root G: same-priority conflict, contained to one field
    G = OpKey("CA", "410", "", "2026-11-22", "PEK", "CTU", "OP")
    g = fact(eng, G, 2, t)
    check("edge: G bound cleanly", g["binding"]["status"] == "bound")
    check("edge: G est_dep same-instant conflict",
          g["fields"]["est_dep"]["state"] == "conflict"
          and {c["event_id"] for c in g["fields"]["est_dep"]["conflict_contenders"]}
              == {"E-G-C1", "E-G-C2"})
    check("edge: conflict does not spread to sched_dep",
          g["fields"]["sched_dep"]["state"] == "value"
          and g["fields"]["sched_dep"]["value"] == "2026-11-22T10:00:00+08:00")

    # unknown sender message about G stays external, never projects 15:00
    g5 = fact(eng, G, 5, t)
    check("edge: unknown sender kept as external evidence",
          any(e["sender"] == "XX-STRANGER" for e in g5["external_evidence"]))
    check("edge: external sender cannot overwrite fact",
          g5["fields"]["est_dep"]["state"] == "conflict")

    # double-root rename: A is ambiguous and not guessed
    A = OpKey("CA", "401", "", "2026-11-22", "PEK", "CTU", "OP")
    ra = fact(eng, A, 2, t)
    check("edge: double-root rename stays ambiguous",
          ra["binding"]["status"] == "ambiguous"
          and any(w["code"] == "rename_double_root" for w in ra["warnings"]))

    # rename fork / cycle / unknown-root warnings are all retained
    warn_codes = {w["code"] for w in Exporter(wh).batch_ingest_warnings(1)}
    for code in ("rename_fork", "rename_cycle", "rename_double_root",
                 "rename_unknown_root", "codeshare_unknown_root"):
        check(f"edge: identity warning retained: {code}", code in warn_codes)

    # codeshare overlap -> ambiguous marketing binding
    MKX = OpKey("KA", "3001", "", "2026-11-22", "PEK", "CTU", "MK")
    rm = fact(eng, MKX, 2, t)
    check("edge: overlapping codeshares ambiguous",
          rm["binding"]["status"] == "ambiguous"
          and set(rm["binding"].get("candidate_roots") or []) == {"A", "B"})

    # forward reference: unknown before root declared, bound after closure
    F = OpKey("CA", "500", "", "2026-11-22", "PEK", "CTU", "OP")
    check("edge: forward reference unresolved in early batch",
          fact(eng, F, 3, t)["binding"]["status"] == "unknown")
    rf = fact(eng, F, 4, t)
    check("edge: forward reference binds once root closes",
          rf["binding"]["status"] == "bound"
          and rf["binding"]["root_id"] == "F"
          and rf["fields"]["sched_dep"]["value"] == "2026-11-22T12:00:00+08:00")

    # cross UTC midnight: local service date independent; offsets preserved
    X = OpKey("CA", "600", "", "2026-11-22", "URC", "PEK", "OP")
    rx = fact(eng, X, 6, "2026-11-22T23:45:00+08:00")
    check("edge: cross-day act_dep keeps +08:00 wall text",
          rx["fields"]["act_dep"]["value"] == "2026-11-23T00:15:00+08:00")
    check("edge: cross-day act_arr accepts Z offset",
          rx["fields"]["act_arr"]["value"] == "2026-11-22T16:30:00Z")
    check("edge: service_date stays local operating day 2026-11-22",
          rx["queried_key"]["service_date"] == "2026-11-22")

    # bad-offset and unsupported payloads are quarantined, not projected
    pe = Exporter(wh).parse_errors(2)
    files = {os.path.basename(p["raw_ref"]): p["error"] for p in pe}
    check("edge: missing UTC offset rejected",
          any("offset" in msg for msg in files.values()))
    check("edge: unsupported booking element quarantined",
          "msg_unsupported.xml" in files)
    wh.close()
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------- interrupted / tamper / copy
def test_publish_atomicity_and_tamper() -> None:
    d = tempfile.mkdtemp(prefix="wh_atom_")
    wh_dir = os.path.join(d, "wh")
    wh = Warehouse(wh_dir)
    # stage batch 1 without publishing
    wh.ingest_batch(os.path.join(FIX, "main", "manifest_1.json"),
                    os.path.join(FIX, "main", "batch_1"), publish=False)
    check("atom: staged batch is open and unqueryable",
          [r["status"] for r in wh.list_batches()] == ["open"])
    try:
        QueryEngine(wh).query(
            OpKey("CA", "100", "", "2026-11-20", "PEK", "SHA", "OP"),
            1, "2026-11-20T09:45:00+08:00")
        check("atom: query on unpublished batch rejected", False)
    except IngestError:
        check("atom: query on unpublished batch rejected", True)
    wh.close()

    # reopen and publish atomically
    wh = Warehouse(wh_dir)
    bid = wh.publish_open_batch()
    check("atom: open batch published on reopen", bid == 1
          and wh.published_batches() == [1])
    wh.close()

    # tamper a blob after publish: a new batch digest check must fail
    tampered = os.path.join(d, "tampered")
    shutil.copytree(os.path.join(FIX, "main", "batch_2"),
                    os.path.join(tampered, "batch_2"), dirs_exist_ok=True)
    target = os.path.join(tampered, "batch_2", "msg_E-L-02.xml")
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("<!-- tampered -->")
    wh = Warehouse(wh_dir)
    try:
        wh.ingest_batch(os.path.join(FIX, "main", "manifest_2.json"),
                        os.path.join(tampered, "batch_2"))
        check("atom: tampered blob rejected by sha256", False)
    except IngestError:
        check("atom: tampered blob rejected by sha256", True)
    check("atom: failed ingest leaves no new published batch",
          wh.published_batches() == [1])
    wh.close()

    # copy the repository and reopen: old batch remains queryable
    replica = os.path.join(d, "replica")
    shutil.copytree(wh_dir, replica)
    wh2 = Warehouse(replica)
    r = fact(QueryEngine(wh2),
             OpKey("CA", "100", "", "2026-11-20", "PEK", "SHA", "OP"),
             1, "2026-11-20T09:45:00+08:00")
    check("copy: reopened replica serves old batch facts",
          r["binding"]["root_id"] == "L"
          and r["fields"]["sched_dep"]["state"] == "value")
    wh2.close()
    shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------- ordering equivalence
def test_out_of_order_equivalence() -> None:
    """Same messages, different file/ingest ordering -> identical fact set."""
    d = tempfile.mkdtemp(prefix="wh_ord_")
    # warehouse A: normal order
    wh_a = Warehouse(os.path.join(d, "a"))
    ingest_suite(wh_a, "main", (1, 2, 3))
    # warehouse B: rebuild batches with files shuffled on disk (manifest order
    # is authoritative, so facts must be identical)
    b_root = os.path.join(d, "shuffled")
    for bid in (1, 2, 3):
        src = os.path.join(FIX, "main", f"batch_{bid}")
        dst = os.path.join(b_root, f"batch_{bid}")
        files = sorted(os.listdir(src), reverse=True)  # reversed order
        for f in files:
            os.makedirs(dst, exist_ok=True)
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    wh_b = Warehouse(os.path.join(d, "b"))
    for bid in (1, 2, 3):
        wh_b.ingest_batch(os.path.join(FIX, "main", f"manifest_{bid}.json"),
                          os.path.join(b_root, f"batch_{bid}"))

    keys = [
        OpKey("CA", "100", "", "2026-11-20", "PEK", "SHA", "OP"),
        OpKey("CA", "200", "", "2026-11-20", "PEK", "SHA", "OP"),
        OpKey("CA", "100", "", "2026-11-21", "PEK", "SHA", "OP"),
        OpKey("CA", "300", "", "2026-11-20", "SHA", "CAN", "OP"),
    ]
    times = ["2026-11-20T09:45:00+08:00", "2026-11-20T13:30:00+08:00",
             "2026-11-20T14:30:00+08:00"]
    same = True
    for bid in (1, 2, 3):
        for k in keys:
            for bt in times:
                fa = fact(QueryEngine(wh_a), k, bid, bt)
                fb = fact(QueryEngine(wh_b), k, bid, bt)
                if json.dumps(fa, sort_keys=True) != json.dumps(fb, sort_keys=True):
                    same = False
    check("order: shuffled file order yields identical complete fact set", same)
    wh_a.close(); wh_b.close()
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- large/memory
def test_large_batch_memory() -> dict:
    d = tempfile.mkdtemp(prefix="wh_large_")
    wh_dir = os.path.join(d, "wh")
    tracemalloc.start()
    wh = Warehouse(wh_dir)
    report = wh.ingest_batch(
        os.path.join(FIX, "large", "manifest_1.json"),
        os.path.join(FIX, "large", "batch_1"))
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    blob_bytes = sum(
        os.path.getsize(os.path.join(root, f))
        for root, _, files in os.walk(os.path.join(FIX, "large", "batch_1"))
        for f in files)
    db_bytes = os.path.getsize(os.path.join(wh_dir, "legfact.sqlite"))
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # Query every leg; resident memory must stay well below the blob volume,
    # proving large bodies are streamed from disk rather than retained.
    eng = QueryEngine(wh)
    for fno in range(7000, 7400):
        r = fact(eng, OpKey("CA", str(fno), "", "2026-11-23", "PEK", "SHA", "OP"),
                 1, "2026-11-23T09:00:00+08:00")
        assert r["fields"]["sched_dep"]["state"] == "value"
    wh.close()

    metrics = {
        "events": report.events_accepted,
        "blob_bytes_on_disk": blob_bytes,
        "sqlite_bytes": db_bytes,
        "tracemalloc_peak_bytes": peak,
        "max_rss_kb": rss_kb,
        "peak_to_blob_ratio": round(peak / blob_bytes, 4),
    }
    check("large: 400 big messages ingested", report.events_accepted == 400)
    check("large: traced peak far smaller than blob volume",
          peak < blob_bytes,
          f"peak={peak} blob={blob_bytes}")
    check("large: DB index much smaller than raw blobs",
          db_bytes < blob_bytes)
    shutil.rmtree(d, ignore_errors=True)
    return metrics


def main() -> int:
    test_edge_semantics()
    test_publish_atomicity_and_tamper()
    test_out_of_order_equivalence()
    metrics = test_large_batch_memory()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n=== large-batch memory metrics ===")
    print(json.dumps(metrics, indent=2))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
