"""Command-line surface of the offline arrival-prediction backtest.

Subcommands (machine-readable outputs; errors on stderr with exit 2):

  run        execute one frozen backtest into a fresh output directory
  show       print the run header and grouped/overall metric summary
  samples    stream the per-checkpoint sample detail rows (NDJSON)
  checks     print the denominator-conservation checks

A run only evaluates historical warehouse data; it never predicts new flights
and never writes back to the source warehouse.  Outputs land in a new
directory created atomically (temp directory + rename), so a failed run —
including a structural input rejection — never leaves a result that could be
mistaken for a complete backtest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile

from .backtest_config import BacktestInputError, load_inputs
from .backtest_engine import NdjsonFileSink, run_backtest_streaming
from .storage import IngestError, Warehouse

HEADER_NAME = "header.json"
SUMMARY_NAME = "summary.json"
SAMPLES_NAME = "samples.ndjson"


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _fail(exc: Exception) -> int:
    code = getattr(exc, "code", "error")
    detail = getattr(exc, "detail", str(exc))
    print(json.dumps({"error": code, "detail": detail}, ensure_ascii=False),
          file=__import__("sys").stderr)
    return 2


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_run(a: argparse.Namespace) -> int:
    tmpdir = None
    try:
        if os.path.exists(a.out):
            raise BacktestInputError(
                "output_exists",
                f"{a.out} already exists; refusing to overwrite a published "
                f"backtest output directory")
        wh = Warehouse(a.warehouse)
        try:
            max_batch = max(wh.published_batches(), default=0)
            inputs = load_inputs(
                spec_path=a.spec, roster_path=a.roster,
                visibility_path=a.visibility,
                max_published_batch=max_batch)
            stats: dict = {}
            # The sink is created inside the final output directory only
            # after validation succeeds; rows stream to disk one JSON per
            # line so a full-scale run never holds all samples in memory.
            parent = os.path.dirname(os.path.abspath(a.out)) or "."
            os.makedirs(parent, exist_ok=True)
            tmpdir = tempfile.mkdtemp(prefix=".backtest-", dir=parent)
            sink = NdjsonFileSink(os.path.join(tmpdir, SAMPLES_NAME))
            try:
                result = run_backtest_streaming(wh, inputs, sink, stats)
            finally:
                sink.close()
        finally:
            wh.close()

        header = {
            "airport": inputs.spec.airport,
            "close_out_at": inputs.spec.close_text,
            "lead_minutes": inputs.spec.lead_minutes,
            "coverage": {"start": inputs.spec.coverage_start_text,
                         "end": inputs.spec.coverage_end_text},
            "close_out_visible_batch": result["close_batch"],
            "n_roster_roots": len(inputs.roster),
            "n_checkpoints": sink.count,
            "input_files": dict(inputs.files),
            "input_sha256": {
                "run_spec": _sha256_file(a.spec),
                "roster": _sha256_file(a.roster),
                "visibility": _sha256_file(a.visibility)},
            "stream_stats": result["stats"],
        }
        with open(os.path.join(tmpdir, HEADER_NAME), "w",
                  encoding="utf-8") as fh:
            json.dump(header, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(tmpdir, SUMMARY_NAME), "w",
                  encoding="utf-8") as fh:
            json.dump(result["summary"], fh, ensure_ascii=False, indent=2)
        os.replace(tmpdir, os.path.abspath(a.out))
        tmpdir = None
        out = {
            "mode": "run", "out_dir": os.path.abspath(a.out),
            "header": header,
            "conservation": result["summary"]["conservation"],
        }
        _emit(out)
        return 0
    except (BacktestInputError, IngestError, OSError) as exc:
        if tmpdir is not None:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        return _fail(exc)
    except BaseException:
        if tmpdir is not None:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _load(path: str, name: str):
    with open(os.path.join(path, name), encoding="utf-8") as fh:
        return json.load(fh)


def cmd_show(a: argparse.Namespace) -> int:
    try:
        _emit({"header": _load(a.out_dir, HEADER_NAME),
               "summary": _load(a.out_dir, SUMMARY_NAME)})
        return 0
    except OSError as exc:
        return _fail(exc)


def cmd_samples(a: argparse.Namespace) -> int:
    try:
        path = os.path.join(a.out_dir, SAMPLES_NAME)
        with open(path, encoding="utf-8") as fh:
            if a.group:
                for line in fh:
                    d = json.loads(line)
                    if d["group"] == a.group and (
                            a.lead is None or d["lead_minutes"] == a.lead):
                        print(line, end="")
            else:
                for line in fh:
                    if a.lead is None or \
                            json.loads(line)["lead_minutes"] == a.lead:
                        print(line, end="")
        return 0
    except OSError as exc:
        return _fail(exc)


def cmd_checks(a: argparse.Namespace) -> int:
    try:
        summary = _load(a.out_dir, SUMMARY_NAME)
        _emit(summary["conservation"])
        return 0 if summary["conservation"]["checks"]["all_conservation_ok"] \
            else 1
    except OSError as exc:
        return _fail(exc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="legfact backtest",
        description="Offline arrival-prediction backtest (historical only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("run", help="run one frozen backtest")
    sp.add_argument("--warehouse", required=True)
    sp.add_argument("--spec", required=True, help="frozen run spec JSON")
    sp.add_argument("--roster", required=True,
                    help="frozen observation roster NDJSON")
    sp.add_argument("--visibility", required=True,
                    help="duty-system batch visibility table NDJSON")
    sp.add_argument("--out", required=True,
                    help="output directory (must not already exist)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("show", help="header and metric summary")
    sp.add_argument("--out-dir", required=True)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("samples", help="per-checkpoint sample detail NDJSON")
    sp.add_argument("--out-dir", required=True)
    sp.add_argument("--group")
    sp.add_argument("--lead", type=int)
    sp.set_defaults(func=cmd_samples)

    sp = sub.add_parser("checks", help="denominator conservation checks")
    sp.add_argument("--out-dir", required=True)
    sp.set_defaults(func=cmd_checks)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
