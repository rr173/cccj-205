#!/usr/bin/env python3
"""Command-line interface for the offline leg-fact warehouse.

Subcommands:
  ingest      receive and (optionally) publish a complete handover batch
  publish     finish a staged open batch atomically
  abort       mark an open batch aborted
  batches     list batch states
  query       leg facts at (receipt_batch, business_time)
  identity    explain the identity association chain for a key
  export      export an original source message with provenance
  warnings    list identity/ingest warnings of a batch
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from legfact import (Exporter, IngestError, OpKey, QueryEngine,  # noqa: E402
                     QueryError, Warehouse)


def _key_from_args(a, kind: str) -> OpKey:
    return OpKey(a.carrier, a.flight_no, a.suffix or "", a.service_date,
                 a.departure, a.arrival, kind)


def cmd_ingest(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    try:
        report = wh.ingest_batch(a.manifest, a.files_root, publish=not a.stage)
    except IngestError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        wh.close()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_publish(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    try:
        bid = wh.publish_open_batch()
    except IngestError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        wh.close()
    print(json.dumps({"published_batch": bid}, indent=2))
    return 0


def cmd_abort(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    bid = wh.abort_open_batch()
    wh.close()
    print(json.dumps({"aborted_batch": bid}, indent=2))
    return 0


def cmd_batches(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    rows = [dict(r) for r in wh.list_batches()]
    wh.close()
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_query(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    eng = QueryEngine(wh)
    kind = "MK" if a.marketing else "OP"
    key = _key_from_args(a, kind)
    try:
        res = eng.query(key, a.receipt_batch, a.business_time)
    except QueryError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        wh.close()
    print(json.dumps(QueryEngine.to_dict(res), ensure_ascii=False, indent=2))
    return 0


def cmd_identity(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    ex = Exporter(wh)
    kind = "MK" if a.marketing else "OP"
    key = _key_from_args(a, kind)
    out = ex.explain_identity(key, a.receipt_batch, a.identity_at)
    wh.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    ex = Exporter(wh)
    try:
        em = ex.export_event(a.event_id, a.receipt_batch)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        wh.close()
    out = {
        "event_id": em.event_id, "receipt_batch": em.receipt_batch,
        "raw_rel_path": em.raw_rel_path, "sha256": em.sha256,
        "manifest_entry": em.manifest_entry, "envelope": em.envelope,
        "fields": em.fields, "cancellations": em.cancellations,
        "unsupported": em.unsupported, "binding": em.binding,
        "raw_xml": em.raw_xml,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_warnings(a: argparse.Namespace) -> int:
    wh = Warehouse(a.warehouse)
    ex = Exporter(wh)
    out = {"identity_warnings": ex.batch_ingest_warnings(a.receipt_batch),
           "parse_errors": ex.parse_errors(a.receipt_batch)}
    wh.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Offline leg-fact warehouse CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_key(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--carrier", required=True)
        sp.add_argument("--flight-no", required=True)
        sp.add_argument("--suffix", default="")
        sp.add_argument("--service-date", required=True,
                        help="local operating day YYYY-MM-DD (independent field)")
        sp.add_argument("--departure", required=True)
        sp.add_argument("--arrival", required=True)

    sp = sub.add_parser("ingest")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--files-root", required=True)
    sp.add_argument("--stage", action="store_true",
                    help="load as open batch without publishing")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("publish")
    sp.add_argument("--warehouse", required=True)
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("abort")
    sp.add_argument("--warehouse", required=True)
    sp.set_defaults(func=cmd_abort)

    sp = sub.add_parser("batches")
    sp.add_argument("--warehouse", required=True)
    sp.set_defaults(func=cmd_batches)

    sp = sub.add_parser("query")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--receipt-batch", type=int, required=True)
    sp.add_argument("--business-time", required=True,
                    help="ISO-8601 with explicit UTC offset")
    sp.add_argument("--marketing", action="store_true",
                    help="key is a marketing (codeshare) key")
    add_key(sp)
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("identity")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--receipt-batch", type=int, required=True)
    sp.add_argument("--identity-at", required=True)
    sp.add_argument("--marketing", action="store_true")
    add_key(sp)
    sp.set_defaults(func=cmd_identity)

    sp = sub.add_parser("export")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--event-id", required=True)
    sp.add_argument("--receipt-batch", type=int)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("warnings")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--receipt-batch", type=int, required=True)
    sp.set_defaults(func=cmd_warnings)

    sp = sub.add_parser("mart", help="landing-fee data mart (see docs/landing_mart.md)")
    sp.add_argument("mart_args", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_mart)

    sp = sub.add_parser("mart-verify",
                        help="re-derive landing ledger invariants from stored rows")
    sp.add_argument("--book", required=True)
    sp.set_defaults(func=cmd_mart_verify)

    sp = sub.add_parser("stand",
                        help="offline stand-occupancy reconciliation (see "
                             "docs/stand_reconciliation.md)")
    sp.add_argument("stand_args", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_stand)

    sp = sub.add_parser("backtest",
                        help="offline arrival-prediction backtest (see "
                             "docs/arrival_backtest.md)")
    sp.add_argument("backtest_args", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_backtest)
    return p


def cmd_backtest(a: argparse.Namespace) -> int:
    from legfact.backtest_cli import main as backtest_main
    return backtest_main(list(a.backtest_args))


def cmd_stand(a: argparse.Namespace) -> int:
    from legfact.stand_cli import main as stand_main
    return stand_main(list(a.stand_args))


def cmd_mart(a: argparse.Namespace) -> int:
    from legfact.landing_cli import main as mart_main
    return mart_main(list(a.mart_args))


def cmd_mart_verify(a: argparse.Namespace) -> int:
    from legfact.landing_cli import verify_main
    return verify_main(["--book", a.book])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
