"""Command-line surface of the offline stand-occupancy reconciliation.

Subcommands (machine-readable JSON on stdout, errors on stderr with exit 2):

  run        reconcile one audit window into a new SQLite sidecar
  show       run header, totals, per-unit durations and bounds
  segments   gap-free per-unit time ledger (optionally one unit)
  evidence   drill-down for one physical unit at one instant
  issues     inputs that could not participate, with reasons and locations

The run reads its four NDJSON inputs and a published legfact warehouse; it
never writes back to the source warehouse and never infers stands from gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os

from .stand_config import StandInputError, load_inputs
from .stand_engine import SCHEMA, ReconError, run_reconciliation
from .stand_store import (BatchingSegmentSink, ReconBook,
                          _populate_header_and_inputs)
from .storage import IngestError, Warehouse


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _fail(exc: Exception) -> int:
    code = getattr(exc, "code", "error")
    detail = getattr(exc, "detail", str(exc))
    print(json.dumps({"error": code, "detail": detail}, ensure_ascii=False),
          file=sys_stderr())
    return 2


def sys_stderr():
    import sys
    return sys.stderr


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_run(a: argparse.Namespace) -> int:
    import sqlite3
    import tempfile
    try:
        d = os.path.dirname(os.path.abspath(a.out)) or "."
        os.makedirs(d, exist_ok=True)
        if os.path.exists(a.out):
            raise ReconError(
                "output_exists",
                f"{a.out} already exists; refusing to overwrite a published "
                f"reconciliation sidecar")
        fd, tmp = tempfile.mkstemp(prefix=".recon-", suffix=".sqlite", dir=d)
        os.close(fd)
        # One database connection holds both the scratch source table and the
        # published sidecar tables, so source locations never sit in Python
        # memory and no cross-connection attach/detach lock can occur.
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        # A deliberately small page cache and file-backed temp store: the
        # sidecar is written once and never re-read inside the job, so neither
        # a large cache nor in-memory temp tables should inflate the RSS HWM.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-1024")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_spill=ON")
        conn.execute("PRAGMA mmap_size=0")
        inputs = load_inputs(
            roster_path=a.roster, ledger_path=a.ledger,
            map_path=a.map, sensor_path=a.sensor, scratch_conn=conn)
        meta = {"files": dict(inputs.files),
                "sha256": {role: _sha256_file(p) for role, p in (
                    ("visit_roster", a.roster), ("usage_ledger", a.ledger),
                    ("stand_map", a.map), ("sensor", a.sensor))}}
        wh = Warehouse(a.warehouse)
        try:
            conn.executescript(SCHEMA)
            conn.execute("DROP INDEX IF EXISTS idx_seg_unit")
            sink = BatchingSegmentSink(conn, batch_size=250)
            result = run_reconciliation(
                wh, inputs, airport=a.airport,
                window_start=a.window_start, window_end=a.window_end,
                receipt_batch=a.receipt_batch, cutoff=a.cutoff,
                segment_sink=sink)
            sink.flush()
            conn.execute(
                "CREATE INDEX idx_seg_unit ON recon_segments(unit_id, start_us)")
            _populate_header_and_inputs(conn, result, meta)
            conn.execute("DROP TABLE IF EXISTS scratch_sources")
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row[0] != "ok":  # pragma: no cover
                raise ReconError("sqlite_integrity", row[0])
            conn.commit()
        finally:
            wh.close()
        conn.close()
        os.replace(tmp, os.path.abspath(a.out))
        out = {
            "mode": "run", "sidecar": os.path.abspath(a.out),
            "airport": result["airport"], "window": result["window"],
            "receipt_batch": result["receipt_batch"],
            "cutoff": result["cutoff"],
            "counts": {
                "visits_total": result["summary"]["visits_total"],
                "visits_determinable":
                    result["summary"]["visits_determinable"],
                "visits_unavailable": result["summary"]["visits_unavailable"],
                "ledger_records": result["summary"]["ledger_records"],
                "physical_claim_intervals":
                    result["summary"]["physical_claim_intervals"],
                "observation_records":
                    result["summary"]["observation_records"],
                "online_records": result["summary"]["online_records"],
                "issues": result["summary"]["issue_count"],
                "segments": result["summary"]["segment_count"],
            },
            "summary": result["summary"],
            "issues": [
                {"kind": i.kind, "detail": i.detail,
                 "source_id": i.source_id,
                 "visit_id": i.visit_id, "ledger_record_id": i.ledger_record_id,
                 "unit_id": i.unit_id} for i in result["issues"]],
        }
        _emit(out)
        return 0
    except (StandInputError, ReconError, IngestError, OSError) as exc:
        try:
            if "tmp" in locals() and os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return _fail(exc)
    except BaseException:
        try:
            if "tmp" in locals() and os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_show(a: argparse.Namespace) -> int:
    try:
        book = ReconBook(a.sidecar)
        try:
            _emit({"header": book.header(), "summary": book.summary(),
                   "visits": book.visits(), "issues": book.issues()})
        finally:
            book.close()
        return 0
    except (ReconError, OSError) as exc:
        return _fail(exc)


def cmd_segments(a: argparse.Namespace) -> int:
    try:
        book = ReconBook(a.sidecar)
        try:
            _emit({"unit": a.unit, "segments": book.segments(a.unit)})
        finally:
            book.close()
        return 0
    except (ReconError, OSError) as exc:
        return _fail(exc)


def cmd_evidence(a: argparse.Namespace) -> int:
    try:
        book = ReconBook(a.sidecar)
        try:
            _emit(book.evidence_at(a.unit, int(a.at_us)))
        finally:
            book.close()
        return 0
    except (ReconError, OSError) as exc:
        return _fail(exc)


def cmd_issues(a: argparse.Namespace) -> int:
    try:
        book = ReconBook(a.sidecar)
        try:
            _emit({"issues": book.issues(a.kind)})
        finally:
            book.close()
        return 0
    except (ReconError, OSError) as exc:
        return _fail(exc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="legfact stand",
        description="Offline stand-occupancy reconciliation")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="reconcile one audit window")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--airport", required=True)
    sp.add_argument("--window-start", required=True,
                    help="window start, ISO-8601 with explicit offset")
    sp.add_argument("--window-end", required=True,
                    help="window end (exclusive), ISO-8601 with explicit offset")
    sp.add_argument("--receipt-batch", type=int, required=True)
    sp.add_argument("--cutoff", required=True,
                    help="fact cutoff, not earlier than window end, "
                         "ISO-8601 with explicit offset")
    sp.add_argument("--roster", required=True, help="visit roster NDJSON")
    sp.add_argument("--ledger", required=True, help="stand usage ledger NDJSON")
    sp.add_argument("--map", required=True, help="stand map NDJSON")
    sp.add_argument("--sensor", required=True, help="sensor records NDJSON")
    sp.add_argument("--out", required=True, help="output SQLite sidecar path")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("show", help="header, totals and per-unit durations")
    sp.add_argument("--sidecar", required=True)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("segments", help="gap-free per-unit time ledger")
    sp.add_argument("--sidecar", required=True)
    sp.add_argument("--unit")
    sp.set_defaults(func=cmd_segments)

    sp = sub.add_parser("evidence",
                        help="drill-down for one unit at one integer-US instant")
    sp.add_argument("--sidecar", required=True)
    sp.add_argument("--unit", required=True)
    sp.add_argument("--at-us", required=True,
                    help="absolute instant as integer microseconds since epoch")
    sp.set_defaults(func=cmd_evidence)

    sp = sub.add_parser("issues", help="non-participating inputs and reasons")
    sp.add_argument("--sidecar", required=True)
    sp.add_argument("--kind")
    sp.set_defaults(func=cmd_issues)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
