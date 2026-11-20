"""Command-line surface of the landing-fee data mart (``legfact-cli mart ...``).

Subcommands (machine-readable JSON on stdout, errors on stderr with exit 2):

  init      open one airport account book
  show      print the book configuration and published version history
  plan      dry-run a calculation (writes nothing)
  release   atomically publish a new version with its journal entries
  verify    re-derive ledger invariants from stored rows

All monetary values are integers in minor currency units and are never
converted to float.
"""
from __future__ import annotations

import argparse
import json
import sys

from .landing import LandingBook, MartError, plan, release, verify
from .storage import IngestError


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _fail(exc: Exception) -> int:
    code = getattr(exc, "code", "error")
    detail = getattr(exc, "detail", str(exc))
    print(json.dumps({"error": code, "detail": detail}, ensure_ascii=False),
          file=sys.stderr)
    return 2


def _add_calc(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--book", required=True)
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--receipt-batch", type=int, required=True)
    sp.add_argument("--cutoff", required=True,
                    help="fact cutoff as ISO-8601 with explicit UTC offset")
    sp.add_argument("--roster", required=True,
                    help="full landing-meter roster JSON")
    sp.add_argument("--tariff", required=True, help="full tariff JSON")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="legfact mart",
                                description="Landing-fee data mart")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="open one airport account book")
    sp.add_argument("--book", required=True)
    sp.add_argument("--config", required=True,
                    help="book configuration JSON (airport/timezone/months/currency)")

    sp = sub.add_parser("show", help="show book config and versions")
    sp.add_argument("--book", required=True)
    sp.add_argument("--version", type=int,
                    help="include that version's lines and journal entries")
    sp.add_argument("--journal", action="store_true",
                    help="include every journal entry of every published "
                         "version (append-only history)")

    sp = sub.add_parser("plan", help="dry run; changes nothing")
    _add_calc(sp)

    sp = sub.add_parser("release", help="publish a new version atomically")
    _add_calc(sp)
    sp.add_argument("--request-id", required=True)
    sp.add_argument("--base-version", type=int, required=True,
                    help="published head version this calculation replaces "
                         "(0 for the first release)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        try:
            with open(args.config, "rb") as fh:
                doc = json.loads(fh.read().decode("utf-8"))
            cfg = LandingBook.init_book(args.book, doc)
        except (OSError, json.JSONDecodeError, MartError) as exc:
            return _fail(exc)
        _emit({"mode": "init", "book": cfg.to_dict()})
        return 0

    if args.cmd == "show":
        try:
            book = LandingBook(args.book)
            try:
                out = {"book": book.config().to_dict(),
                       "head_version": book.head_version(),
                       "versions": [v["summary"] for v in book.versions()],
                       "effective": book.effective_summary()}
                if args.journal:
                    out["journal"] = book.entries()
                if args.version is not None:
                    out["detail"] = {
                        "version": book.version(args.version)["summary"],
                        "lines": book.lines_for_summary(args.version),
                        "entries": book.entries(args.version),
                        "changes_by_attribution": book.changes_by_attribution(
                            args.version),
                    }
                _emit(out)
            finally:
                book.close()
            return 0
        except (MartError, OSError) as exc:
            return _fail(exc)

    if args.cmd == "plan":
        try:
            _emit(plan(args.warehouse, args.book, args.receipt_batch,
                       args.cutoff, args.roster, args.tariff))
            return 0
        except (MartError, IngestError) as exc:
            return _fail(exc)

    if args.cmd == "release":
        try:
            # CLI uses 0 for "no previous version" (the first release); the
            # API expresses that as None so a real version id can never be 0.
            base = None if args.base_version == 0 else args.base_version
            _emit(release(args.warehouse, args.book, args.receipt_batch,
                          args.cutoff, args.roster, args.tariff,
                          args.request_id, base))
            return 0
        except (MartError, IngestError) as exc:
            return _fail(exc)

    return _fail(MartError("bad_command", str(args.cmd)))


def verify_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="legfact mart-verify")
    p.add_argument("--book", required=True)
    args = p.parse_args(argv)
    try:
        out = verify(args.book)
    except (MartError, OSError) as exc:
        return _fail(exc)
    _emit(out)
    return 0 if out["ok"] else 1
