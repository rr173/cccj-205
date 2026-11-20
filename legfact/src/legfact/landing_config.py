"""Configuration inputs of the landing-fee data mart.

The airport accountant opens **one account book per airport**.  Opening the
book (``mart init``) fixes the airport, its IANA time zone, the closed set of
settlement months of the book and the settlement currency.  Those four facts
never change for the life of the book.

Every calculation (``mart plan`` / ``mart release``) names

* one **published** legfact ``receipt_batch`` — the knowledge cut,
* one **fact cutoff** carrying an explicit UTC offset — no actual arrival
  after it may be used,
* two *full* dimension files:

  - ``meter_roster`` — one record per operating root in scope.  A root is
    identified by the legfact ``initial_leg`` operating key (carrier, flight
    number, suffix, service date, departure, arrival), never by anything the
    tariff or the accountant renames it to.  Each record gives the settlement
    account, the weight in kilogram and the tariff category.  Being the
    *scope* of the run, a root removed from the roster is simply not billed.
  - ``tariff`` — for each tariff category, half-open local-date validity
    intervals in the airport's own local date, each carrying a non-negative
    integer amount **in minor currency units per billable unit**; plus, for
    each account, an in-month serial-number ladder: increasing inclusive upper
    bounds with a multiplier expressed in ten-thousandths (``10000`` = x1).
    The last rung has no upper bound and acts as the fallback.

Structural problems (duplicate roots in the roster, non-positive or
non-integer weight, overlapping tariff validity intervals, a ladder without a
fallback rung, malformed serial records...) reject the whole calculation:
the run fails before anything is published.

Identifiers
-----------
``account``        opaque settlement account code (non-empty string)
``root_id``        legfact operating root (``initial_leg`` root_id)
``root``           the legfact initial operating key, six fields as a JSON object
``category``       opaque tariff category code (non-empty string)
``serial``         1-based in-month position per account/local month
``weight_kg``      positive integer kilogram
``bulk_units``     ``ceil(weight_kg / 1000)`` (at least 1)
``min_amount``     amount in minor currency units, integer, rounded only at the
                   final per-line amount
``ten_thousandths`` multiplier integer, ``10000`` means x1

Range versus validity
---------------------
The month of a landing is decided by the **local date of the actual arrival
instant**.  A landing whose local date is not inside one of the book's
settlement months stays visible as ``out_of_range`` but is never charged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

try:  # Python >= 3.9
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python 3.11 is required anyway
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConfigError(ValueError):
    """A structural problem in the book configuration or a dimension file."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def load_zone(name: str):
    if ZoneInfo is None:
        raise ConfigError("bad_timezone", "zoneinfo is not available")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ConfigError("bad_timezone",
                          f"IANA time zone {name!r} is unknown: {exc}") from exc


def _local_date(text: str, what: str):
    from datetime import date
    if not isinstance(text, str) or not _DATE_RE.match(text):
        raise ConfigError("bad_dimension",
                          f"{what}: expected YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError("bad_dimension", f"{what}: {exc}") from exc


# --------------------------------------------------------------------- book
@dataclass(frozen=True)
class BookConfig:
    airport: str
    timezone: str
    months: tuple[str, ...]
    currency: str

    @staticmethod
    def from_dict(doc: dict) -> "BookConfig":
        if not isinstance(doc, dict):
            raise ConfigError("bad_book", "book configuration must be an object")
        missing = [k for k in ("airport", "timezone", "months", "currency")
                   if k not in doc]
        if missing:
            raise ConfigError("bad_book",
                              f"book configuration missing {missing}")
        airport = doc["airport"]
        if not isinstance(airport, str) or not re.match(r"^[A-Za-z0-9]{3,5}$",
                                                       airport):
            raise ConfigError("bad_book",
                              f"airport must be a 3-5 character IATA/ICAO code, "
                              f"got {airport!r}")
        tzname = doc["timezone"]
        if not isinstance(tzname, str) or not tzname:
            raise ConfigError("bad_timezone", f"got {tzname!r}")
        load_zone(tzname)  # validates
        months_raw = doc["months"]
        if not isinstance(months_raw, list) or not months_raw:
            raise ConfigError("bad_book", "months must be a non-empty array")
        months = []
        for m in months_raw:
            if not isinstance(m, str) or not _MONTH_RE.match(m):
                raise ConfigError("bad_book",
                                  f"month must be YYYY-MM, got {m!r}")
            months.append(m)
        if len(set(months)) != len(months):
            raise ConfigError("bad_book", "duplicate settlement month")
        months.sort()
        currency = doc["currency"]
        if not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            raise ConfigError("bad_book",
                              f"currency must be an ISO-4217-like code, got "
                              f"{currency!r}")
        return BookConfig(airport=airport, timezone=tzname,
                          months=tuple(months), currency=currency)

    def to_dict(self) -> dict:
        return {"airport": self.airport, "timezone": self.timezone,
                "months": list(self.months), "currency": self.currency}

    def month_of_local_date(self, d) -> Optional[str]:
        m = f"{d.year:04d}-{d.month:02d}"
        return m if m in self.months else None


# -------------------------------------------------------------------- roster
@dataclass(frozen=True)
class RosterRecord:
    root_id: str
    root_key: tuple          # (carrier, flight_no, suffix, service_date,
                             #  departure, arrival)
    account: str
    weight_kg: int
    category: str
    source_line: int

    @property
    def root_key_dict(self) -> dict:
        c, f, s, sd, dep, arr = self.root_key
        return {"carrier": c, "flight_no": f, "suffix": s,
                "service_date": sd, "departure": dep, "arrival": arr}

    def to_dict(self) -> dict:
        return {"root_id": self.root_id, "root": self.root_key_dict,
                "account": self.account, "weight_kg": self.weight_kg,
                "category": self.category}


_KEY_FIELDS = ("carrier", "flight_no", "suffix", "service_date",
               "departure", "arrival")


def root_key_tuple(d: dict, what: str) -> tuple:
    if not isinstance(d, dict):
        raise ConfigError("bad_roster", f"{what}: root must be an object")
    missing = [k for k in _KEY_FIELDS if k not in d]
    if missing:
        raise ConfigError("bad_roster", f"{what}: root missing {missing}")
    sd = d["service_date"]
    _local_date(sd, f"{what} service_date")
    return (str(d["carrier"]), str(d["flight_no"]), str(d.get("suffix") or ""),
            sd, str(d["departure"]), str(d["arrival"]))


def _expect_array(doc, key: str, what: str) -> list:
    if not isinstance(doc, dict):
        raise ConfigError("bad_dimension", f"{what} must be a JSON object")
    v = doc.get(key)
    if not isinstance(v, list):
        raise ConfigError("bad_dimension", f"{what} must contain an array {key!r}")
    return v


def parse_roster(doc, airport: str) -> dict[str, RosterRecord]:
    """Parse and structurally validate the full landing-meter roster."""
    records = _expect_array(doc, "roots", "meter_roster")
    out: dict[str, RosterRecord] = {}
    key_owner: dict[tuple, str] = {}
    for i, rec in enumerate(records):
        what = f"meter_roster.roots[{i}]"
        if not isinstance(rec, dict):
            raise ConfigError("bad_roster", f"{what} must be an object")
        for k in ("root_id", "root", "account", "weight_kg", "category"):
            if k not in rec:
                raise ConfigError("bad_roster", f"{what} missing {k!r}")
        root_id = rec["root_id"]
        if not isinstance(root_id, str) or not root_id.strip():
            raise ConfigError("bad_roster",
                              f"{what}: root_id must be a non-empty string")
        if root_id in out:
            raise ConfigError(
                "duplicate_root",
                f"{what}: root_id {root_id!r} appears more than once")
        key = root_key_tuple(rec["root"], what)
        if key in key_owner:
            raise ConfigError(
                "duplicate_root_key",
                f"{what}: operating key already claimed by root "
                f"{key_owner[key]!r}")
        key_owner[key] = root_id
        account = rec["account"]
        if not isinstance(account, str) or not account.strip():
            raise ConfigError("bad_roster",
                              f"{what}: account must be a non-empty string")
        weight = rec["weight_kg"]
        if not _is_int(weight) or weight <= 0:
            raise ConfigError(
                "invalid_weight",
                f"{what}: weight_kg must be a positive integer, got {weight!r}")
        category = rec["category"]
        if not isinstance(category, str) or not category.strip():
            raise ConfigError("bad_roster",
                              f"{what}: category must be a non-empty string")
        out[root_id] = RosterRecord(
            root_id=root_id, root_key=key, account=account,
            weight_kg=weight, category=category, source_line=i)
    return out


# -------------------------------------------------------------------- tariff
@dataclass(frozen=True)
class TariffEntry:
    category: str
    valid_from: str          # local date, inclusive
    valid_to: str            # local date, exclusive
    min_per_unit: int
    source_line: int

    def to_dict(self) -> dict:
        return {"category": self.category, "valid_from": self.valid_from,
                "valid_to": self.valid_to, "min_per_unit": self.min_per_unit}


@dataclass(frozen=True)
class SerialRung:
    upto_serial: Optional[int]   # None = fallback / last rung
    ten_thousandths: int
    source_line: int

    def to_dict(self) -> dict:
        return {"upto_serial": self.upto_serial,
                "ten_thousandths": self.ten_thousandths}

    def matches(self, serial: int) -> bool:
        return self.upto_serial is None or serial <= self.upto_serial


@dataclass
class Tariff:
    airport: str
    currency: str
    entries: dict[str, list[TariffEntry]] = field(default_factory=dict)
    ladders: dict[str, list[SerialRung]] = field(default_factory=dict)

    def entry_for(self, category: str, local_date: str) -> Optional[TariffEntry]:
        for e in self.entries.get(category, ()):  # sorted; count is tiny
            if e.valid_from <= local_date < e.valid_to:
                return e
        return None

    def rung_for(self, account: str, serial: int) -> Optional[SerialRung]:
        for r in self.ladders.get(account, ()):
            if r.matches(serial):
                return r
        return None

    def categories(self) -> list[str]:
        return sorted(self.entries)

    def accounts(self) -> list[str]:
        return sorted(self.ladders)


def parse_tariff(doc, airport: str, currency: str) -> Tariff:
    if not isinstance(doc, dict):
        raise ConfigError("bad_dimension", "tariff must be a JSON object")
    if doc.get("airport", airport) != airport:
        raise ConfigError(
            "tariff_airport_mismatch",
            f"tariff is for {doc.get('airport')!r}, book is for {airport!r}")
    if doc.get("currency", currency) != currency:
        raise ConfigError(
            "tariff_currency_mismatch",
            f"tariff is in {doc.get('currency')!r}, book is in {currency!r}")

    tariff = Tariff(airport=airport, currency=currency)

    rates = doc.get("rates")
    if not isinstance(rates, list):
        raise ConfigError("bad_dimension", "tariff must contain an array 'rates'")
    for i, rec in enumerate(rates):
        what = f"tariff.rates[{i}]"
        if not isinstance(rec, dict):
            raise ConfigError("bad_dimension", f"{what} must be an object")
        for k in ("category", "valid_from", "valid_to", "min_per_unit"):
            if k not in rec:
                raise ConfigError("bad_dimension", f"{what} missing {k!r}")
        cat = rec["category"]
        if not isinstance(cat, str) or not cat.strip():
            raise ConfigError("bad_dimension",
                              f"{what}: category must be a non-empty string")
        vf = rec["valid_from"]
        vt = rec["valid_to"]
        df = _local_date(vf, f"{what}.valid_from")
        dt = _local_date(vt, f"{what}.valid_to")
        if not df < dt:
            raise ConfigError(
                "bad_tariff_interval",
                f"{what}: valid_from {vf} must be strictly before valid_to {vt}")
        rate = rec["min_per_unit"]
        if not _is_int(rate) or rate < 0:
            raise ConfigError(
                "bad_rate",
                f"{what}: min_per_unit must be a non-negative integer, "
                f"got {rate!r}")
        tariff.entries.setdefault(cat, []).append(
            TariffEntry(cat, vf, vt, rate, i))

    # overlap check per category: intervals are half-open [from, to)
    for cat, ents in tariff.entries.items():
        ents.sort(key=lambda e: (e.valid_from, e.valid_to))
        for prev, nxt in zip(ents, ents[1:]):
            if nxt.valid_from < prev.valid_to:
                raise ConfigError(
                    "overlapping_tariff",
                    f"category {cat!r}: interval starting {nxt.valid_from} "
                    f"overlaps the one starting {prev.valid_from} "
                    f"(ends {prev.valid_to})")
        tariff.entries[cat] = ents

    serials = doc.get("serials")
    if not isinstance(serials, list):
        raise ConfigError("bad_dimension", "tariff must contain an array 'serials'")
    for i, rec in enumerate(serials):
        what = f"tariff.serials[{i}]"
        if not isinstance(rec, dict):
            raise ConfigError("bad_dimension", f"{what} must be an object")
        for k in ("account", "rungs"):
            if k not in rec:
                raise ConfigError("bad_dimension", f"{what} missing {k!r}")
        acct = rec["account"]
        if not isinstance(acct, str) or not acct.strip():
            raise ConfigError("bad_dimension",
                              f"{what}: account must be a non-empty string")
        if acct in tariff.ladders:
            raise ConfigError("duplicate_ladder",
                              f"{what}: account {acct!r} has two ladders")
        rungs_raw = rec["rungs"]
        if not isinstance(rungs_raw, list) or not rungs_raw:
            raise ConfigError(
                "ladder_without_fallback",
                f"{what}: account {acct!r} has an empty ladder")
        rungs: list[SerialRung] = []
        prev_upto = 0
        for j, r in enumerate(rungs_raw):
            rw = f"{what}.rungs[{j}]"
            if not isinstance(r, dict):
                raise ConfigError("bad_dimension", f"{rw} must be an object")
            if "upto_serial" not in r or "ten_thousandths" not in r:
                raise ConfigError("bad_dimension",
                                  f"{rw} needs upto_serial and ten_thousandths")
            up = r["upto_serial"]
            if up is not None:
                if not _is_int(up) or up <= 0:
                    raise ConfigError(
                        "bad_ladder",
                        f"{rw}: upto_serial must be a positive integer, got {up!r}")
                if up <= prev_upto:
                    raise ConfigError(
                        "bad_ladder",
                        f"{rw}: upto_serial {up} must be greater than the "
                        f"previous rung's {prev_upto}")
                prev_upto = up
            mult = r["ten_thousandths"]
            if not _is_int(mult) or mult < 0:
                raise ConfigError(
                    "bad_ladder",
                    f"{rw}: ten_thousandths must be a non-negative integer")
            rungs.append(SerialRung(up, mult, j))
        if rungs[-1].upto_serial is not None:
            raise ConfigError(
                "ladder_without_fallback",
                f"{what}: last rung of account {acct!r} must have "
                f"upto_serial = null (no upper bound)")
        if any(r.upto_serial is None for r in rungs[:-1]):
            raise ConfigError(
                "bad_ladder",
                f"{what}: only the last rung may have upto_serial = null")
        tariff.ladders[acct] = rungs
    return tariff


def parse_dimension_json(path: str, what: str):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ConfigError("bad_dimension", f"{what}: cannot read {path}: {exc}") from exc
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError("bad_dimension", f"{what} is not valid JSON: {exc}") from exc
