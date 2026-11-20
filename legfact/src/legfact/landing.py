"""Landing-fee data mart on top of the legfact warehouse.

Reads (never writes) a published legfact knowledge cut, adjudicates each
roster root's authoritative **actual arrival** with the warehouse's own
field rules, prices the landing against a synthetic tariff and keeps an
append-only, versioned ledger.

See ``docs/landing_mart.md`` for the contract and the machine-readable
formats; ``landing_config.py`` holds the dimension parsing/validation.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import identity
from .landing_config import (BookConfig, RosterRecord, Tariff, load_zone,
                             parse_dimension_json, parse_roster, parse_tariff)
from .query import resolve_field_updates, FieldResult  # noqa: F401
from .storage import Warehouse
from .timeutil import parse_offset_datetime

# Unpriced reasons (never "zero amount")
REASON_UNKNOWN_ROOT = "unknown_root"
REASON_AIRPORT = "arrival_airport_mismatch"
REASON_NO_ACTUAL = "no_definite_actual"
REASON_AFTER_CUTOFF = "actual_after_cutoff"
REASON_OUT_OF_RANGE = "month_outside_book"

STATUS_PRICED = "priced"
STATUS_UNPRICED = "unpriced"
STATUS_OUT_OF_RANGE = "out_of_range"

TEN_THOUSAND = 10000

SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_book (
  singleton     INTEGER PRIMARY KEY CHECK (singleton = 1),
  airport       TEXT NOT NULL,
  timezone      TEXT NOT NULL,
  months_json   TEXT NOT NULL,
  currency      TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS landing_versions (
  version_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id          TEXT NOT NULL UNIQUE,
  fingerprint         TEXT NOT NULL,
  base_version        INTEGER,
  receipt_batch       INTEGER NOT NULL,
  cutoff_text         TEXT NOT NULL,
  cutoff_utc          TEXT NOT NULL,
  roster_json         TEXT NOT NULL,
  roster_sha256       TEXT NOT NULL,
  tariff_json         TEXT NOT NULL,
  tariff_sha256       TEXT NOT NULL,
  priced_count        INTEGER NOT NULL,
  unpriced_count      INTEGER NOT NULL,
  out_of_range_count  INTEGER NOT NULL,
  effective_total_minor INTEGER NOT NULL,
  entry_count         INTEGER NOT NULL,
  entry_total_minor   INTEGER NOT NULL,
  created_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS landing_lines (
  version_id          INTEGER NOT NULL,
  seq                 INTEGER NOT NULL,
  root_id             TEXT NOT NULL,
  root_key_json       TEXT NOT NULL,
  status              TEXT NOT NULL,
  reason              TEXT,
  reason_detail       TEXT,
  account             TEXT,
  category            TEXT,
  weight_kg           INTEGER,
  bulk_units          INTEGER,
  actual_value        TEXT,
  actual_utc          TEXT,
  local_date          TEXT,
  month               TEXT,
  rate_from           TEXT,
  rate_to             TEXT,
  rate_min_per_unit   INTEGER,
  serial              INTEGER,
  rung_upto           INTEGER,
  ten_thousandths     INTEGER,
  numerator           TEXT,
  denominator         TEXT,
  amount_minor        INTEGER,
  evidence_json       TEXT NOT NULL,
  posting_id          INTEGER,
  PRIMARY KEY (version_id, seq)
);
CREATE TABLE IF NOT EXISTS landing_postings (
  posting_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  root_id           TEXT NOT NULL,
  account           TEXT NOT NULL,
  month             TEXT NOT NULL,
  amount_minor      INTEGER NOT NULL,
  created_version   INTEGER NOT NULL,
  created_entry_id  INTEGER NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('open','reversed')),
  reversed_version  INTEGER,
  reversed_entry_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_postings_open
  ON landing_postings(root_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS landing_entries (
  entry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id        INTEGER NOT NULL,
  kind              TEXT NOT NULL CHECK (kind IN ('positive','reversal')),
  posting_id        INTEGER NOT NULL,
  reverses_entry_id INTEGER,
  root_id           TEXT NOT NULL,
  account           TEXT NOT NULL,
  month             TEXT NOT NULL,
  amount_minor      INTEGER NOT NULL,
  note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_version
  ON landing_entries(version_id, entry_id);
CREATE INDEX IF NOT EXISTS idx_lines_status
  ON landing_lines(version_id, status);
"""


class MartError(Exception):
    code = "mart_error"

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


class InputRejected(MartError):
    """Structural problem: reject the whole calculation, write nothing."""


class ReleaseConflict(MartError):
    pass


# ----------------------------------------------------------------- utilities
def _canonical_json(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ceil_second(dt: datetime) -> datetime:
    if dt.microsecond == 0:
        return dt
    return dt.replace(microsecond=0) + timedelta(seconds=1)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round_half_up(numerator: int, denominator: int) -> int:
    """Round a non-negative rational to the nearest integer, halves up."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("_round_half_up expects non-negative rational")
    return (2 * numerator + denominator) // (2 * denominator)


def _bulk_units(weight_kg: int) -> int:
    return (weight_kg + 999) // 1000


# ---------------------------------------------------------------- computation
@dataclass(slots=True)
class ComputedLine:
    seq: int
    root_id: str
    root_key: dict
    status: str
    reason: Optional[str] = None
    reason_detail: Optional[str] = None
    account: Optional[str] = None
    category: Optional[str] = None
    weight_kg: Optional[int] = None
    bulk_units: Optional[int] = None
    actual_value: Optional[str] = None
    actual_utc: Optional[str] = None
    local_date: Optional[str] = None
    month: Optional[str] = None
    rate_from: Optional[str] = None
    rate_to: Optional[str] = None
    rate_min_per_unit: Optional[int] = None
    serial: Optional[int] = None
    rung_upto: Optional[int] = None
    ten_thousandths: Optional[int] = None
    numerator: Optional[int] = None
    denominator: Optional[int] = None
    amount_minor: Optional[int] = None
    evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "seq": self.seq, "root_id": self.root_id, "root": self.root_key,
            "status": self.status, "reason": self.reason,
            "reason_detail": self.reason_detail,
            "account": self.account, "category": self.category,
            "weight_kg": self.weight_kg, "bulk_units": self.bulk_units,
            "actual_arrival": {
                "value": self.actual_value, "utc": self.actual_utc,
                "local_date": self.local_date},
            "month": self.month, "serial": self.serial,
            "tariff_entry": None if self.rate_from is None else {
                "valid_from": self.rate_from, "valid_to": self.rate_to,
                "min_per_unit": self.rate_min_per_unit},
            "serial_rung": None if self.ten_thousandths is None else {
                "upto_serial": self.rung_upto,
                "ten_thousandths": self.ten_thousandths},
            "calculation": None if self.amount_minor is None else {
                "bulk_units": self.bulk_units,
                "rate_min_per_unit": self.rate_min_per_unit,
                "ten_thousandths": self.ten_thousandths,
                "numerator": self.numerator, "denominator": self.denominator,
                "amount_minor": self.amount_minor},
            "amount_minor": self.amount_minor,
            "evidence": self.evidence,
        }
        return d


@dataclass
class Computation:
    lines: list[ComputedLine]
    priced: dict[str, ComputedLine]       # root_id -> line (account/month/amount)
    summary: dict


def _evidence(event_id: str, batch_id: int, sender: str, revision: int,
              effective_at: str, raw_ref: str, xml_location: Optional[str],
              line: Optional[int], column: Optional[int]) -> dict:
    return {"event_id": event_id, "receipt_batch": batch_id,
            "sender": sender, "source_revision": revision,
            "effective_at": effective_at, "raw_file": raw_ref,
            "xml_location": xml_location, "line": line, "column": column}


_eff_cache: dict[str, datetime] = {}


def _scan_actuals(wh: Warehouse, batch_id: int, cutoff: datetime,
                  roster_ids: set[str], stats: Optional[dict] = None
                  ) -> dict[str, list]:
    """One pass over the knowledge cut, never re-scanning per roster row.

    ``stats`` (if given) is filled with the number of SQL statements and
    candidate rows examined, so callers can prove the scan is not repeated
    per roster row.
    """
    graph = wh.load_graph(batch_id)
    conn = wh._conn
    stmt_count = [0]

    def _trace(_sql):
        stmt_count[0] += 1

    conn.set_trace_callback(_trace)
    try:
        return _scan_actuals_inner(wh, conn, graph, batch_id, cutoff,
                                   roster_ids, stats, stmt_count)
    finally:
        conn.set_trace_callback(None)


def _scan_actuals_inner(wh, conn, graph, batch_id, cutoff, roster_ids, stats,
                        stmt_count) -> dict[str, list]:
    ceiling = _iso_z(_ceil_second(cutoff))
    cur = conn.execute(
        "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
        "e.effective_at, e.identity_at, e.raw_ref, "
        "k.carrier, k.flight_no, k.suffix, k.service_date, k.departure, k.arrival, "
        "f.state, f.value, f.xml_location, f.line, f.columnn "
        "FROM event_fields f "
        "JOIN events e ON e.event_id=f.event_id AND e.batch_id=f.batch_id "
        "JOIN event_keys k ON k.event_id=e.event_id AND k.batch_id=e.batch_id "
        "WHERE f.field='act_arr' AND e.batch_id<=? AND e.effective_at<=? "
        "ORDER BY e.effective_at, e.source_revision, e.event_id, e.batch_id",
        (batch_id, ceiling))

    # Per root we keep only the rows tied at the current winning priority
    # (greatest effective_at, then source_revision).  Rows are streamed in
    # ascending priority order, so a higher-priority row simply replaces the
    # bucket, and a lower-priority row is discarded immediately.
    winners: dict[str, list] = {}   # root -> [eff, rev, {payload: ev_tuple}]
    bind_cache: dict[bytes, Optional[str]] = {}
    n_rows = 0
    n_examined = 0
    # Stream the cursor: no fetchall.  Per root we retain only
    #   (eff, rev, {payload: (raw_value, event_id, batch_id, xml_loc, line, col)})
    # so a root with many sparse reports costs a handful of small tuples, not
    # one dict per message.  A repeated identical payload keeps only the
    # first evidence, which is what the field rule reports anyway.
    for r in cur:
        n_rows += 1
        eff = r["effective_at"]
        if len(_eff_cache) > 100000:
            _eff_cache.clear()
        if eff in _eff_cache:
            eff_utc = _eff_cache[eff]
        else:
            eff_utc = parse_offset_datetime(eff).utc
            _eff_cache[eff] = eff_utc
        if eff_utc > cutoff:
            continue
        # One bind per distinct (operating key, identity instant, sender):
        # identity_at is empty in practice but is part of the key because a
        # late report may legitimately carry an older identity instant.
        sender = r["sender"]
        bind_key = (r["carrier"] + "\x1f" + r["flight_no"] + "\x1f"
                    + r["suffix"] + "\x1f" + r["service_date"] + "\x1f"
                    + r["departure"] + "\x1f" + r["arrival"] + "\x1f"
                    + r["identity_at"] + "\x1f" + sender).encode()
        if bind_key in bind_cache:
            root = bind_cache[bind_key]
        else:
            key = identity.OpKey(
                r["carrier"], r["flight_no"], r["suffix"], r["service_date"],
                r["departure"], r["arrival"], "OP")
            b = graph.bind_operating(key,
                                     parse_offset_datetime(r["identity_at"]))
            root = (b.root_id if b.status == "bound"
                    and b.sender == sender else None)
            bind_cache[bind_key] = root
        if root is None or root not in roster_ids:
            continue
        n_examined += 1
        rev = r["source_revision"]
        bucket = winners.get(root)
        payload = (r["state"], r["value"])
        # A raw value of None (an explicit clear) already carries its state in
        # the payload key; no extra allocation is needed for the value slot.
        ev = (payload[1], r["event_id"], r["batch_id"], r["xml_location"],
              r["line"], r["columnn"])
        if bucket is None:
            winners[root] = [eff, rev, {payload: ev}]
            continue
        if eff > bucket[0] or (eff == bucket[0] and rev > bucket[1]):
            bucket[0], bucket[1] = eff, rev
            bucket[2] = {payload: ev}
        elif eff == bucket[0] and rev == bucket[1]:
            bucket[2].setdefault(payload, ev)
    if stats is not None:
        stats["sql_statements"] = stmt_count[0]
        stats["candidate_act_arr_rows"] = n_rows
        stats["winning_rows"] = n_examined
        stats["roster_roots"] = len(roster_ids)
        stats["distinct_bound_key_candidates"] = len(bind_cache)
        stats["roots_with_actual_candidates"] = len(winners)
    return winners


def _adjudicate_actual(bucket: Optional[list]) -> FieldResult:
    """Feed candidate payloads to the warehouse's canonical field rule.

    ``bucket`` is ``[effective_at, source_revision, {payload: evidence}]`` for
    the rows tied at a root's winning priority — the same input the query
    engine would derive from the same knowledge cut.  Delegating to
    :func:`legfact.query.resolve_field_updates` keeps exactly one
    implementation of the conflict/clear/absence rules.
    """
    if not bucket:
        return FieldResult("act_arr", "actual_arrival", "absent")
    eff, rev, top = bucket
    vals = [{"state": st, "value": va, "effective_at": eff,
             "source_revision": rev, "evidence": ev}
            for (st, va), ev in top.items()]
    return resolve_field_updates("act_arr", vals)


def compute(wh: Warehouse, book: BookConfig, roster: dict[str, RosterRecord],
            tariff: Tariff, batch_id: int, cutoff_text: str) -> Computation:
    wh.require_published(batch_id)
    try:
        cutoff = parse_offset_datetime(cutoff_text)
    except Exception as exc:  # noqa: BLE001 - normalized to input rejection
        raise InputRejected("bad_cutoff",
                            f"fact cutoff must be ISO-8601 with an explicit "
                            f"offset: {cutoff_text!r} ({exc})") from exc
    cutoff_utc = cutoff.utc
    tz = load_zone(book.timezone)
    graph = wh.load_graph(batch_id)

    scan_stats: dict = {}
    winners = _scan_actuals(wh, batch_id, cutoff_utc, set(roster), scan_stats)

    lines: list[ComputedLine] = []
    for seq, root_id in enumerate(roster, start=1):
        rec = roster[root_id]
        ln = ComputedLine(seq=seq, root_id=root_id,
                          root_key=rec.root_key_dict, status=STATUS_UNPRICED)
        lines.append(ln)
        if root_id not in graph.initials:
            ln.reason = REASON_UNKNOWN_ROOT
            ln.reason_detail = "root_id is not an initial leg in this knowledge cut"
            continue
        il = graph.initials[root_id]
        if il.key is not None and il.key.as_tuple() != tuple(
                rec.root_key) + ("OP",):
            raise InputRejected(
                "roster_key_mismatch",
                f"root {root_id!r} is declared in the knowledge cut with "
                f"operating key {il.key.pretty()!r}, but the roster identifies "
                f"it as {rec.root_key!r}; a roster row must name the actual "
                f"initial operating key")
        if not il.key or il.key.arrival != book.airport:
            ln.reason = REASON_AIRPORT
            ln.reason_detail = (
                f"initial operating key arrival "
                f"{il.key.arrival if il.key else None!r} != book airport "
                f"{book.airport!r}")
            continue

        fres = _adjudicate_actual(winners.pop(root_id, None))
        if fres.state != "value" or not fres.value:
            ln.reason = REASON_NO_ACTUAL
            ln.reason_detail = fres.state  # absent | cleared | conflict
            if fres.state == "conflict":
                ln.evidence = [c["evidence"] for c in (fres.contenders or [])]
            elif fres.winning is not None:
                ln.evidence = [fres.winning["evidence"]]
            continue
        win = fres.winning
        try:
            actual = parse_offset_datetime(fres.value)
        except Exception as exc:  # pragma: no cover - parser already vetted
            ln.reason = REASON_NO_ACTUAL
            ln.reason_detail = f"unparseable actual arrival: {exc}"
            continue
        actual_utc = actual.utc
        ln.actual_value = fres.value
        ln.actual_utc = actual.iso_utc()
        ln.evidence = [win["evidence"]]
        if actual_utc > cutoff_utc:
            ln.reason = REASON_AFTER_CUTOFF
            ln.reason_detail = (f"actual arrival {actual.iso_utc()} is after "
                                f"fact cutoff {cutoff.iso_utc()}")
            continue

        local_date = actual_utc.astimezone(tz).date().isoformat()
        ln.local_date = local_date
        month = book.month_of_local_date(actual_utc.astimezone(tz).date())
        if month is None:
            ln.status = STATUS_OUT_OF_RANGE
            ln.reason = REASON_OUT_OF_RANGE
            ln.reason_detail = (f"local date {local_date} is outside settlement "
                                f"months {list(book.months)}")
            continue
        ln.month = month
        ln.account = rec.account
        ln.category = rec.category
        ln.weight_kg = rec.weight_kg
        ln.bulk_units = _bulk_units(rec.weight_kg)
        ln.status = STATUS_PRICED

    # ---- serial numbers per (account, local month), among priced lines.
    # Ordering: actual UTC instant, then stable operating-root id ascending.
    # Category/date changes never reset the counter.
    groups: dict[tuple[str, str], list[ComputedLine]] = {}
    for ln in lines:
        if ln.status == STATUS_PRICED:
            groups.setdefault((ln.account, ln.month), []).append(ln)
    for (_acct, _month), group in groups.items():
        group.sort(key=lambda x: (x.actual_utc, x.root_id))
        for i, ln in enumerate(group, start=1):
            ln.serial = i

    # ---- rate + ladder completeness: reject the whole run, never bill 0.
    for ln in lines:
        if ln.status != STATUS_PRICED:
            continue
        entry = tariff.entry_for(ln.category, ln.local_date)
        if entry is None:
            raise InputRejected(
                "no_tariff_entry",
                f"root {ln.root_id!r} landing on local date {ln.local_date} "
                f"in category {ln.category!r} has no tariff interval")
        rung = tariff.rung_for(ln.account, ln.serial)
        if rung is None:
            raise InputRejected(
                "no_serial_rung",
                f"root {ln.root_id!r} account {ln.account!r} serial {ln.serial} "
                f"has no ladder rung")
        ln.rate_from, ln.rate_to = entry.valid_from, entry.valid_to
        ln.rate_min_per_unit = entry.min_per_unit
        ln.rung_upto = rung.upto_serial
        ln.ten_thousandths = rung.ten_thousandths
        ln.numerator = ln.bulk_units * entry.min_per_unit * rung.ten_thousandths
        ln.denominator = TEN_THOUSAND
        ln.amount_minor = _round_half_up(ln.numerator, ln.denominator)

    priced = {ln.root_id: ln for ln in lines if ln.status == STATUS_PRICED}
    counts = {
        STATUS_PRICED: len(priced),
        STATUS_UNPRICED: sum(1 for l in lines if l.status == STATUS_UNPRICED),
        STATUS_OUT_OF_RANGE: sum(1 for l in lines
                                 if l.status == STATUS_OUT_OF_RANGE),
    }
    by_account_month: dict[tuple[str, str], int] = {}
    for ln in priced.values():
        k = (ln.account, ln.month)
        by_account_month[k] = by_account_month.get(k, 0) + ln.amount_minor
    summary = {
        "roster_count": len(roster),
        "priced_count": counts[STATUS_PRICED],
        "unpriced_count": counts[STATUS_UNPRICED],
        "out_of_range_count": counts[STATUS_OUT_OF_RANGE],
        "total_minor": sum(l.amount_minor for l in priced.values()),
        "currency": book.currency,
        "accounts": sorted({l.account for l in priced.values()}),
        "balances": [
            {"account": a, "month": m, "amount_minor": v}
            for (a, m), v in sorted(by_account_month.items())],
        "unpriced_by_reason": _count_reasons(lines),
        "scan_stats": scan_stats,
        "receipt_batch": batch_id,
        "cutoff": cutoff.iso_utc(),
        "cutoff_text": cutoff_text,
    }
    return Computation(lines=lines, priced=priced, summary=summary)


def _count_reasons(lines: list[ComputedLine]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ln in lines:
        if ln.status == STATUS_UNPRICED and ln.reason:
            out[ln.reason] = out.get(ln.reason, 0) + 1
    return out


# --------------------------------------------------------------------- store
class LandingBook:
    """One airport account book: configuration plus published versions."""

    def __init__(self, book_dir: str):
        self.root = os.path.abspath(book_dir)
        os.makedirs(self.root, exist_ok=True)
        self.db_path = os.path.join(self.root, "landing.sqlite")
        self.conn = sqlite3.connect(self.db_path, isolation_level=None,
                                    timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ open book
    @staticmethod
    def init_book(book_dir: str, config_doc: dict) -> BookConfig:
        cfg = BookConfig.from_dict(config_doc)
        book = LandingBook(book_dir)
        try:
            row = book.conn.execute(
                "SELECT * FROM landing_book WHERE singleton=1").fetchone()
            if row is not None:
                existing = BookConfig(
                    airport=row["airport"], timezone=row["timezone"],
                    months=tuple(json.loads(row["months_json"])),
                    currency=row["currency"])
                if existing != cfg:
                    raise MartError(
                        "book_config_conflict",
                        f"book already opened as {existing.to_dict()}; "
                        f"refusing to reopen as {cfg.to_dict()}")
                return existing
            book.conn.execute(
                "INSERT INTO landing_book(singleton,airport,timezone,"
                "months_json,currency,created_at) VALUES(1,?,?,?,?,datetime('now'))",
                (cfg.airport, cfg.timezone, _canonical_json(list(cfg.months)),
                 cfg.currency))
            return cfg
        finally:
            book.close()

    def config(self) -> BookConfig:
        row = self.conn.execute(
            "SELECT * FROM landing_book WHERE singleton=1").fetchone()
        if row is None:
            raise MartError("book_not_open", f"{self.root} has no landing book")
        return BookConfig(airport=row["airport"], timezone=row["timezone"],
                          months=tuple(json.loads(row["months_json"])),
                          currency=row["currency"])

    # --------------------------------------------------------------- queries
    def head_version(self) -> Optional[int]:
        row = self.conn.execute(
            "SELECT MAX(version_id) AS v FROM landing_versions").fetchone()
        return row["v"]

    def version(self, version_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM landing_versions WHERE version_id=?",
            (version_id,)).fetchone()
        if row is None:
            raise MartError("unknown_version", f"version {version_id} not found")
        return _version_row(row)

    def versions(self) -> list[dict]:
        return [_version_row(r) for r in self.conn.execute(
            "SELECT * FROM landing_versions ORDER BY version_id")]

    def lines(self, version_id: int) -> list[dict]:
        self.version(version_id)  # existence check
        out = []
        for r in self.conn.execute(
                "SELECT * FROM landing_lines WHERE version_id=? ORDER BY seq",
                (version_id,)):
            d = dict(r)
            d["root_key"] = json.loads(d.pop("root_key_json"))
            d["evidence"] = json.loads(d.pop("evidence_json"))
            out.append(d)
        return out

    def lines_for_summary(self, version_id: int) -> list[dict]:
        """Raw stored detail rows (evidence as canonical JSON text)."""
        out = []
        for r in self.conn.execute(
                "SELECT * FROM landing_lines WHERE version_id=? ORDER BY seq",
                (version_id,)):
            d = dict(r)
            d["root_key"] = json.loads(d.pop("root_key_json"))
            d["evidence"] = json.loads(d.pop("evidence_json"))
            out.append(d)
        return out

    def entries(self, version_id: Optional[int] = None) -> list[dict]:
        if version_id is None:
            rows = self.conn.execute(
                "SELECT * FROM landing_entries ORDER BY entry_id")
        else:
            self.version(version_id)
            rows = self.conn.execute(
                "SELECT * FROM landing_entries WHERE version_id=? ORDER BY entry_id",
                (version_id,))
        return [dict(r) for r in rows]

    def open_postings(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM landing_postings WHERE status='open' "
            "ORDER BY account, month, root_id")]

    def effective_summary(self, version_id: Optional[int] = None) -> dict:
        """Effective (unreversed) postings as of a published version.

        A posting is effective at version V when it was created at or before V
        and was not reversed at or before V.  The ledger's completed versions
        are append-only, so this equals "open postings now" for the head.
        """
        if version_id is None:
            version_id = self.head_version()
        if version_id is None:
            return {"version_id": None, "total_minor": 0, "balances": []}
        self.version(version_id)
        rows = self.conn.execute(
            "SELECT * FROM landing_postings WHERE created_version<=? "
            "AND (reversed_version IS NULL OR reversed_version>?) "
            "ORDER BY account, month, root_id",
            (version_id, version_id)).fetchall()
        total = 0
        balances: dict[tuple[str, str], int] = {}
        for r in rows:
            total += r["amount_minor"]
            balances[(r["account"], r["month"])] = (
                balances.get((r["account"], r["month"]), 0) + r["amount_minor"])
        return {
            "version_id": version_id,
            "total_minor": total,
            "balances": [{"account": a, "month": m, "amount_minor": v}
                         for (a, m), v in sorted(balances.items())],
        }

    def cumulative_signed(self, version_id: Optional[int] = None) -> int:
        if version_id is None:
            version_id = self.head_version()
        if version_id is None:
            return 0
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor),0) AS s FROM landing_entries "
            "WHERE version_id<=?", (version_id,)).fetchone()
        return int(row["s"])

    def changes_by_attribution(self, version_id: int) -> list[dict]:
        """Version changes grouped by the ORIGINAL account/month attribution."""
        self.version(version_id)
        rows = self.conn.execute(
            "SELECT account, month, kind, COUNT(*) AS n, "
            "SUM(amount_minor) AS amount FROM landing_entries "
            "WHERE version_id=? GROUP BY account, month, kind "
            "ORDER BY account, month, kind", (version_id,)).fetchall()
        merged: dict[tuple[str, str], dict] = {}
        for r in rows:
            d = merged.setdefault((r["account"], r["month"]),
                                  {"account": r["account"], "month": r["month"],
                                   "positive_count": 0, "reversal_count": 0,
                                   "net_minor": 0})
            if r["kind"] == "positive":
                d["positive_count"] += r["n"]
            else:
                d["reversal_count"] += r["n"]
            d["net_minor"] += int(r["amount"] or 0)
        return [merged[k] for k in sorted(merged)]


def _version_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["summary"] = {
        "version_id": d["version_id"], "request_id": d["request_id"],
        "base_version": d["base_version"], "receipt_batch": d["receipt_batch"],
        "cutoff": d["cutoff_utc"], "cutoff_text": d["cutoff_text"],
        "priced_count": d["priced_count"],
        "unpriced_count": d["unpriced_count"],
        "out_of_range_count": d["out_of_range_count"],
        "effective_total_minor": d["effective_total_minor"],
        "entry_count": d["entry_count"],
        "entry_total_minor": d["entry_total_minor"],
        "created_at": d["created_at"],
    }
    return d


# ----------------------------------------------------------- plan / release
def _load_inputs(cfg: BookConfig, roster_path: str, tariff_path: str):
    roster_doc = parse_dimension_json(roster_path, "meter_roster")
    tariff_doc = parse_dimension_json(tariff_path, "tariff")
    if isinstance(roster_doc, dict) and roster_doc.get("airport") not in (
            None, cfg.airport):
        raise InputRejected(
            "roster_airport_mismatch",
            f"roster is for {roster_doc.get('airport')!r}, "
            f"book is for {cfg.airport!r}")
    roster = parse_roster(roster_doc, cfg.airport)
    tariff = parse_tariff(tariff_doc, cfg.airport, cfg.currency)
    roster_json = _canonical_json(roster_doc)
    tariff_json = _canonical_json(tariff_doc)
    return roster, tariff, roster_json, tariff_json


def _fingerprint(base_version, receipt_batch, cutoff_text, roster_sha,
                 tariff_sha) -> str:
    return _sha256_text(_canonical_json({
        "base_version": base_version,
        "receipt_batch": receipt_batch,
        "cutoff": cutoff_text.strip(),
        "roster_sha256": roster_sha,
        "tariff_sha256": tariff_sha,
    }))


def _diff_vs_open(computation: Computation, open_postings: list[dict]) -> dict:
    """Preview the entries a release would append (no writes)."""
    priced = computation.priced
    open_by_root = {p["root_id"]: p for p in open_postings}
    reversals, positives, unchanged = [], [], []
    for p in open_postings:
        root = p["root_id"]
        ln = priced.get(root)
        if ln is None:
            reversals.append({"root_id": root, "posting_id": p["posting_id"],
                              "account": p["account"], "month": p["month"],
                              "amount_minor": -p["amount_minor"],
                              "reason": "root is no longer priceable"})
        elif (ln.account, ln.month, ln.amount_minor) != (
                p["account"], p["month"], p["amount_minor"]):
            reversals.append({
                "root_id": root, "posting_id": p["posting_id"],
                "account": p["account"], "month": p["month"],
                "amount_minor": -p["amount_minor"],
                "reason": "account, month or amount changed"})
            positives.append({"root_id": root, "account": ln.account,
                              "month": ln.month,
                              "amount_minor": ln.amount_minor})
        else:
            unchanged.append({"root_id": root, "posting_id": p["posting_id"]})
    for root, ln in priced.items():
        if root not in open_by_root:
            positives.append({"root_id": root, "account": ln.account,
                              "month": ln.month,
                              "amount_minor": ln.amount_minor})
    net = (sum(e["amount_minor"] for e in reversals)
           + sum(e["amount_minor"] for e in positives))
    return {"reversals": reversals, "positives": positives,
            "unchanged_count": len(unchanged),
            "net_minor": net,
            "entry_count": len(reversals) + len(positives)}


def plan(warehouse_dir: str, book_dir: str, receipt_batch: int,
         cutoff: str, roster_path: str, tariff_path: str) -> dict:
    book = LandingBook(book_dir)
    try:
        cfg = book.config()
        wh = Warehouse(warehouse_dir)
        try:
            roster, tariff, roster_json, tariff_json = _load_inputs(
                cfg, roster_path, tariff_path)
            comp = compute(wh, cfg, roster, tariff, receipt_batch, cutoff)
        finally:
            wh.close()
        diff = _diff_vs_open(comp, book.open_postings())
        return {
            "mode": "plan",
            "book": cfg.to_dict(),
            "base_version": book.head_version(),
            "inputs": {
                "receipt_batch": receipt_batch,
                "cutoff": cutoff,
                "roster_sha256": _sha256_text(roster_json),
                "tariff_sha256": _sha256_text(tariff_json),
            },
            "summary": comp.summary,
            "projected_changes": diff,
            "lines": [ln.to_dict() for ln in comp.lines],
        }
    finally:
        book.close()


def release(warehouse_dir: str, book_dir: str, receipt_batch: int, cutoff: str,
            roster_path: str, tariff_path: str, request_id: str,
            base_version: Optional[int]) -> dict:
    if not isinstance(request_id, str) or not request_id.strip():
        raise InputRejected("bad_request_id", "request_id must be non-empty")
    book = LandingBook(book_dir)
    try:
        cfg = book.config()
        roster, tariff, roster_json, tariff_json = _load_inputs(
            cfg, roster_path, tariff_path)
        roster_sha = _sha256_text(roster_json)
        tariff_sha = _sha256_text(tariff_json)
        fingerprint = _fingerprint(base_version, receipt_batch,
                                   cutoff, roster_sha, tariff_sha)

        # Replay of an already-completed request returns the original version,
        # before any staleness check (a retry after a crash must be safe).
        existing = book.conn.execute(
            "SELECT * FROM landing_versions WHERE request_id=?",
            (request_id,)).fetchone()
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ReleaseConflict(
                    "request_conflict",
                    f"request_id {request_id!r} was already used with different "
                    f"inputs (version {existing['version_id']})")
            return {"mode": "release", "replayed": True,
                    **_release_view(book, existing["version_id"])}

        wh = Warehouse(warehouse_dir)
        try:
            comp = compute(wh, cfg, roster, tariff, receipt_batch, cutoff)
        finally:
            wh.close()

        book.conn.execute("BEGIN IMMEDIATE")
        try:
            head = book.head_version()
            if base_version != head:
                raise ReleaseConflict(
                    "stale_base_version",
                    f"release declares base version {base_version!r} but the "
                    f"published head is {head!r}")
            # Re-check idempotency under the write lock.
            existing = book.conn.execute(
                "SELECT * FROM landing_versions WHERE request_id=?",
                (request_id,)).fetchone()
            if existing is not None:
                book.conn.execute("ROLLBACK")
                if existing["fingerprint"] != fingerprint:
                    raise ReleaseConflict(
                        "request_conflict",
                        f"request_id {request_id!r} already used with "
                        f"different inputs")
                return {"mode": "release", "replayed": True,
                        **_release_view(book, existing["version_id"])}
            vid = _write_version(book, comp, cfg, base_version, request_id,
                                 fingerprint, receipt_batch, cutoff,
                                 roster_json, roster_sha, tariff_json,
                                 tariff_sha)
            book.conn.execute("COMMIT")
        except Exception:
            try:
                book.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return {"mode": "release", "replayed": False,
                **_release_view(book, vid)}
    finally:
        book.close()


def _write_version(book: LandingBook, comp: Computation, cfg: BookConfig,
                   base_version: Optional[int], request_id: str,
                   fingerprint: str, receipt_batch: int, cutoff_text: str,
                   roster_json: str, roster_sha: str, tariff_json: str,
                   tariff_sha: str) -> int:
    conn = book.conn
    priced = comp.priced
    open_postings = {p["root_id"]: dict(p) for p in book.open_postings()}

    # plan entries against effective postings
    entries: list[dict] = []
    root_to_posting: dict[str, int] = {}
    for root, p in open_postings.items():
        ln = priced.get(root)
        if ln is None:
            entries.append({"kind": "reversal", "posting_id": p["posting_id"],
                            "reverses_entry_id": p["created_entry_id"],
                            "root_id": root, "account": p["account"],
                            "month": p["month"],
                            "amount_minor": -p["amount_minor"],
                            "note": "root no longer priceable"})
        elif (ln.account, ln.month, ln.amount_minor) != (
                p["account"], p["month"], p["amount_minor"]):
            entries.append({"kind": "reversal", "posting_id": p["posting_id"],
                            "reverses_entry_id": p["created_entry_id"],
                            "root_id": root, "account": p["account"],
                            "month": p["month"],
                            "amount_minor": -p["amount_minor"],
                            "note": "account, month or amount changed"})
        else:
            # Same economics: keep the posting identity, only the
            # explanation (source/serial/rate notes) is refreshed in the
            # version detail.  No duplicate bookkeeping.
            root_to_posting[root] = p["posting_id"]
    for root, ln in priced.items():
        if root not in root_to_posting and (
                root not in open_postings
                or (ln.account, ln.month, ln.amount_minor)
                != (open_postings[root]["account"],
                    open_postings[root]["month"],
                    open_postings[root]["amount_minor"])):
            entries.append({"kind": "positive", "posting_id": None,
                            "reverses_entry_id": None, "root_id": root,
                            "account": ln.account, "month": ln.month,
                            "amount_minor": ln.amount_minor,
                            "note": "new or changed positive posting"})

    cur = conn.execute(
        "INSERT INTO landing_versions(request_id,fingerprint,base_version,"
        "receipt_batch,cutoff_text,cutoff_utc,roster_json,roster_sha256,"
        "tariff_json,tariff_sha256,priced_count,unpriced_count,"
        "out_of_range_count,effective_total_minor,entry_count,entry_total_minor,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (request_id, fingerprint, base_version, receipt_batch,
         cutoff_text.strip(), comp.summary["cutoff"], roster_json, roster_sha,
         tariff_json, tariff_sha, comp.summary["priced_count"],
         comp.summary["unpriced_count"], comp.summary["out_of_range_count"],
         0, 0, 0))
    version_id = cur.lastrowid

    # append entries in a deterministic order: reversals first, then positives,
    # both by (account, month, root_id) -- the reversal of a root must exist
    # before its replacement positive is inserted.
    entries.sort(key=lambda e: (0 if e["kind"] == "reversal" else 1,
                                e["account"], e["month"], e["root_id"]))
    entry_ids: list[int] = []
    for e in entries:
        if e["kind"] == "reversal":
            posting_id = e["posting_id"]
            c = conn.execute(
                "INSERT INTO landing_entries(version_id,kind,posting_id,"
                "reverses_entry_id,root_id,account,month,amount_minor,note) "
                "VALUES(?,'reversal',?,?,?,?,?,?,?)",
                (version_id, posting_id, e["reverses_entry_id"], e["root_id"],
                 e["account"], e["month"], e["amount_minor"], e["note"]))
            eid = c.lastrowid
            entry_ids.append(eid)
            conn.execute(
                "UPDATE landing_postings SET status='reversed', "
                "reversed_version=?, reversed_entry_id=? WHERE posting_id=? "
                "AND status='open'", (version_id, eid, posting_id))
            if conn.execute("SELECT changes() AS c").fetchone()["c"] != 1:
                raise MartError(
                    "ledger_inconsistent",
                    f"posting {posting_id} was already reversed")
        else:
            ln = priced[e["root_id"]]
            c = conn.execute(
                "INSERT INTO landing_postings(root_id,account,month,"
                "amount_minor,created_version,created_entry_id,status) "
                "VALUES(?,?,?,?,?,0,'open')",
                (ln.root_id, ln.account, ln.month, ln.amount_minor, version_id))
            posting_id = c.lastrowid
            c = conn.execute(
                "INSERT INTO landing_entries(version_id,kind,posting_id,"
                "reverses_entry_id,root_id,account,month,amount_minor,note) "
                "VALUES(?,'positive',?,NULL,?,?,?,?,?)",
                (version_id, posting_id, ln.root_id, ln.account, ln.month,
                 ln.amount_minor, e["note"]))
            eid = c.lastrowid
            entry_ids.append(eid)
            conn.execute(
                "UPDATE landing_postings SET created_entry_id=? "
                "WHERE posting_id=?", (eid, posting_id))
            root_to_posting[ln.root_id] = posting_id

    # version detail -- immutable snapshot of this version's explanation
    for ln in comp.lines:
        posting_id = root_to_posting.get(ln.root_id)
        conn.execute(
            "INSERT INTO landing_lines(version_id,seq,root_id,root_key_json,"
            "status,reason,reason_detail,account,category,weight_kg,bulk_units,"
            "actual_value,actual_utc,local_date,month,rate_from,rate_to,"
            "rate_min_per_unit,serial,rung_upto,ten_thousandths,numerator,"
            "denominator,amount_minor,evidence_json,posting_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, ln.seq, ln.root_id, _canonical_json(ln.root_key),
             ln.status, ln.reason, ln.reason_detail, ln.account, ln.category,
             ln.weight_kg, ln.bulk_units, ln.actual_value, ln.actual_utc,
             ln.local_date, ln.month, ln.rate_from, ln.rate_to,
             ln.rate_min_per_unit, ln.serial, ln.rung_upto,
             ln.ten_thousandths,
             None if ln.numerator is None else str(ln.numerator),
             None if ln.denominator is None else str(ln.denominator),
             ln.amount_minor, _canonical_json(ln.evidence), posting_id))

    eff = book.effective_summary(version_id)
    entry_total = sum(e["amount_minor"] for e in entries)
    conn.execute(
        "UPDATE landing_versions SET effective_total_minor=?, entry_count=?, "
        "entry_total_minor=? WHERE version_id=?",
        (eff["total_minor"], len(entries), entry_total, version_id))
    # ledger invariant: cumulative signed entries == effective total
    if book.cumulative_signed(version_id) != eff["total_minor"]:
        raise MartError(
            "ledger_inconsistent",
            f"cumulative signed entries {book.cumulative_signed(version_id)} "
            f"!= effective total {eff['total_minor']}")
    return version_id


def _release_view(book: LandingBook, version_id: int) -> dict:
    v = book.version(version_id)
    entries = book.entries(version_id)
    return {
        "version": v["summary"],
        "effective": book.effective_summary(version_id),
        "entries": entries,
        "changes_by_attribution": book.changes_by_attribution(version_id),
    }


# ------------------------------------------------------------------- verify
def verify(book_dir: str) -> dict:
    """Re-derive ledger invariants from stored rows only."""
    book = LandingBook(book_dir)
    try:
        versions = book.versions()
        problems: list[dict] = []
        cum = 0
        running: list[dict] = []
        all_entries = book.entries()
        all_entry_ids = {x["entry_id"] for x in all_entries}
        for v in versions:
            vid = v["version_id"]
            entries = [x for x in all_entries if x["version_id"] == vid]
            # the journal and the version detail must be published together
            if len(entries) != v["entry_count"]:
                problems.append({"version_id": vid, "problem":
                                 "journal entry count != version header"})
            detail_postings = {
                l["posting_id"] for l in book.lines_for_summary(vid)
                if l["posting_id"] is not None}
            for e in entries:
                if e["kind"] == "reversal" and e["reverses_entry_id"] not in (
                        all_entry_ids):
                    problems.append({"version_id": vid, "problem":
                                     f"reversal {e['entry_id']} points at "
                                     f"missing entry {e['reverses_entry_id']}"})
                if e["kind"] == "positive" and e["posting_id"] not in (
                        detail_postings):
                    problems.append({"version_id": vid, "problem":
                                     f"positive entry {e['entry_id']} has no "
                                     f"published detail line"})
            cum += sum(e["amount_minor"] for e in entries)
            eff = book.effective_summary(vid)
            running.append({"version_id": vid, "cumulative_signed_minor": cum,
                            "effective_total_minor": eff["total_minor"],
                            "ok": cum == eff["total_minor"]})
            if cum != eff["total_minor"]:
                problems.append({"version_id": vid, "problem":
                                 "cumulative signed != effective total"})
            # version detail must exist and match counts
            lines = book.lines(vid)
            priced = sum(1 for l in lines if l["status"] == "priced")
            if priced != v["priced_count"]:
                problems.append({"version_id": vid, "problem":
                                 "priced line count mismatch"})
        # every open posting must be backed by a positive entry of a
        # published version, and every reversed posting by a reversal entry
        entry_by_id = {x["entry_id"]: x for x in all_entries}
        for p in book.conn.execute("SELECT * FROM landing_postings"):
            created = entry_by_id.get(p["created_entry_id"])
            if created is None or created["kind"] != "positive" or (
                    created["version_id"] != p["created_version"]):
                problems.append({"version_id": p["created_version"],
                                 "problem": f"posting {p['posting_id']} has no "
                                            f"published positive entry"})
            if p["status"] == "reversed":
                rev = entry_by_id.get(p["reversed_entry_id"])
                if rev is None or rev["kind"] != "reversal":
                    problems.append({"version_id": p["reversed_version"],
                                     "problem": f"posting {p['posting_id']} is "
                                                f"reversed without a reversal "
                                                f"entry"})
        # every open posting appears in the head detail with the same posting id
        head = book.head_version()
        if head is not None:
            detail = {l["root_id"]: l for l in book.lines(head)}
            for p in book.open_postings():
                ln = detail.get(p["root_id"])
                if ln is None or ln["posting_id"] != p["posting_id"] or (
                        ln["amount_minor"] != p["amount_minor"]):
                    problems.append({"version_id": head, "problem":
                                     f"open posting {p['posting_id']} not "
                                     f"mirrored by head detail"})
        return {"book": book.config().to_dict(),
                "head_version": head,
                "versions": running,
                "problems": problems,
                "ok": not problems}
    finally:
        book.close()
