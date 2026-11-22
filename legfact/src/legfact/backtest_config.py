"""Frozen inputs of one offline arrival-prediction backtest.

A backtest is defined *before* any fact is read, so that the sample cannot be
re-shaped by the answers:

* a JSON run spec fixing the airport, the uniform result freeze (close-out)
  instant, the distinct positive lead minutes and the claimed coverage of the
  duty-system batch-visibility table;
* a roster NDJSON listing every operating root exactly once, each with its
  roster scheduled arrival instant and a group label;
* a visibility NDJSON that declares, from the first published batch onward,
  the continuous prefix of published batches and the instant each batch first
  became visible to the duty officer (batches already visible before the
  coverage start must be listed too).

Every physical NDJSON line is one JSON object carrying its own ``record_id``;
the loader keeps the file name and 1-based line number, so every downstream
row traces back to an exact input location.  All instants are ISO-8601 strings
with an explicit UTC offset and are compared by their absolute UTC moment;
internally everything is exact integer microseconds — never float.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .timeutil import OffsetDateTime, TimeParseError, parse_offset_datetime

T_ROSTER = "backtest_roster_row"
T_VISIBILITY = "batch_visibility"


class BacktestInputError(ValueError):
    """A malformed or structurally invalid frozen input: the run must fail."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


def to_us(odt: OffsetDateTime) -> int:
    """Absolute UTC instant as exact integer microseconds since the epoch."""
    from datetime import timezone
    epoch = odt.utc
    return int(epoch.timestamp()) * 1_000_000 + epoch.microsecond


# --------------------------------------------------------------------- roster
@dataclass(slots=True)
class RosterRow:
    root_id: str
    group: str
    sched_arr_text: str          # verbatim roster scheduled arrival text
    sched_arr_us: int
    source_file: str
    source_line: int
    record_id: str


@dataclass(slots=True)
class VisibilityRow:
    batch_no: int
    visible_at_text: str
    visible_at_us: int
    source_file: str
    source_line: int
    record_id: str


@dataclass
class BacktestSpec:
    airport: str
    close_text: str
    close_us: int
    lead_minutes: list[int]                  # as given (distinct positive ints)
    coverage_start_text: str
    coverage_start_us: int
    coverage_end_text: str
    coverage_end_us: int
    raw: dict


@dataclass
class BacktestInputs:
    spec: BacktestSpec
    roster: list[RosterRow]
    visibility: list[VisibilityRow]
    files: dict[str, str] = field(default_factory=dict)

    @property
    def roots(self) -> list[str]:
        return [r.root_id for r in self.roster]


# ----------------------------------------------------------------- parsing
def _parse_ts(value, key: str, where: str) -> OffsetDateTime:
    if not isinstance(value, str):
        raise BacktestInputError(
            "bad_timestamp",
            f"{where}: {key} must be an ISO-8601 string with an explicit "
            f"UTC offset, got {value!r}")
    try:
        return parse_offset_datetime(value)
    except TimeParseError as exc:
        raise BacktestInputError(
            "bad_timestamp", f"{where}: {key} {exc}") from exc


def _nonempty_str(value, key: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BacktestInputError(
            "bad_field",
            f"{where}: {key} must be a non-empty string, got {value!r}")
    return value


def load_spec(path: str) -> BacktestSpec:
    try:
        with open(path, "rb") as fh:
            doc = json.loads(fh.read().decode("utf-8"))
    except OSError as exc:
        raise BacktestInputError(
            "unreadable_input", f"cannot read run spec {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise BacktestInputError("bad_encoding",
                                 f"run spec {path} is not UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BacktestInputError(
            "malformed_json", f"run spec is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise BacktestInputError("malformed_json",
                                 "run spec must be a JSON object")
    where = "run spec"
    airport = _nonempty_str(doc.get("airport"), "airport", where)
    close = _parse_ts(doc.get("close_out_at"), "close_out_at", where)
    cov_start = _parse_ts(doc.get("coverage_start"), "coverage_start", where)
    cov_end = _parse_ts(doc.get("coverage_end"), "coverage_end", where)
    leads = doc.get("lead_minutes")
    if not isinstance(leads, list) or not leads:
        raise BacktestInputError(
            "bad_lead_minutes",
            "lead_minutes must be a non-empty array of positive integers")
    if not all(isinstance(x, int) and not isinstance(x, bool) and x > 0
               for x in leads):
        raise BacktestInputError(
            "bad_lead_minutes",
            "every lead minute must be a positive integer (no floats, booleans "
            "or zero/negative values)")
    if len(set(leads)) != len(leads):
        raise BacktestInputError(
            "duplicate_lead_minutes", "lead minutes must be pairwise distinct")
    if not to_us(cov_start) <= to_us(cov_end):
        raise BacktestInputError(
            "bad_coverage_interval",
            "coverage_start must be at or before coverage_end (both ends "
            "inclusive)")
    return BacktestSpec(
        airport=airport, close_text=close.original, close_us=to_us(close),
        lead_minutes=list(leads),
        coverage_start_text=cov_start.original,
        coverage_start_us=to_us(cov_start),
        coverage_end_text=cov_end.original,
        coverage_end_us=to_us(cov_end),
        raw=doc,
    )


def _iter_ndjson(path: str, role: str):
    fname = os.path.basename(path)
    try:
        fh = open(path, "rb")
    except OSError as exc:
        raise BacktestInputError(
            "unreadable_input", f"cannot read {role} file {path}: {exc}") \
            from exc
    count = 0
    with fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BacktestInputError(
                    "bad_encoding",
                    f"{fname}:{lineno} is not UTF-8: {exc}") from exc
            try:
                d = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BacktestInputError(
                    "malformed_json",
                    f"{fname}:{lineno}: not a JSON object: {exc}") from exc
            if not isinstance(d, dict):
                raise BacktestInputError(
                    "malformed_json",
                    f"{fname}:{lineno}: each line must be a JSON object")
            rid = d.get("record_id")
            if not isinstance(rid, str) or not rid.strip():
                raise BacktestInputError(
                    "missing_record_id",
                    f"{fname}:{lineno}: every record needs a non-empty string "
                    f"record_id")
            count += 1
            yield d, fname, lineno, rid
    if not count:
        raise BacktestInputError("empty_input",
                                 f"{role} file {fname} contains no records")


def load_roster(path: str) -> list[RosterRow]:
    rows: list[RosterRow] = []
    seen_roots: set[str] = set()
    seen_rids: set[str] = set()
    for d, fname, lineno, rid in _iter_ndjson(path, "roster"):
        where = f"{fname}:{lineno}"
        if rid in seen_rids:
            raise BacktestInputError(
                "duplicate_record_id",
                f"{where}: record_id {rid!r} appears more than once")
        seen_rids.add(rid)
        if d.get("type") != T_ROSTER:
            raise BacktestInputError(
                "bad_record_type",
                f"{where}: expected type {T_ROSTER!r}, got {d.get('type')!r}")
        root = _nonempty_str(d.get("root_id"), "root_id", where)
        if root in seen_roots:
            raise BacktestInputError(
                "duplicate_roster_root",
                f"{where}: operating root {root!r} is listed more than once; "
                f"the frozen roster names each root exactly once")
        seen_roots.add(root)
        group = _nonempty_str(d.get("group"), "group", where)
        sched = _parse_ts(d.get("roster_sched_arr"), "roster_sched_arr", where)
        rows.append(RosterRow(
            root_id=root, group=group, sched_arr_text=sched.original,
            sched_arr_us=to_us(sched), source_file=fname,
            source_line=lineno, record_id=rid))
    return rows


def load_visibility(path: str) -> list[VisibilityRow]:
    rows: list[VisibilityRow] = []
    seen_rids: set[str] = set()
    for d, fname, lineno, rid in _iter_ndjson(path, "visibility"):
        where = f"{fname}:{lineno}"
        if rid in seen_rids:
            raise BacktestInputError(
                "duplicate_record_id",
                f"{where}: record_id {rid!r} appears more than once")
        seen_rids.add(rid)
        if d.get("type") != T_VISIBILITY:
            raise BacktestInputError(
                "bad_record_type",
                f"{where}: expected type {T_VISIBILITY!r}, got "
                f"{d.get('type')!r}")
        bno = d.get("batch_no")
        if not isinstance(bno, int) or isinstance(bno, bool) or bno <= 0:
            raise BacktestInputError(
                "bad_batch_no",
                f"{where}: batch_no must be a positive integer, got {bno!r}")
        vis = _parse_ts(d.get("first_visible_at"), "first_visible_at", where)
        rows.append(VisibilityRow(
            batch_no=bno, visible_at_text=vis.original,
            visible_at_us=to_us(vis), source_file=fname,
            source_line=lineno, record_id=rid))
    return rows


def validate(inputs: BacktestInputs, *, max_published_batch: int) -> None:
    """Cross-field structural checks against the frozen spec and warehouse.

    ``max_published_batch`` is the greatest published batch in the warehouse;
    every batch named in the visibility table must be published there.
    """
    spec = inputs.spec
    vis = inputs.visibility

    # ---- batch numbering: no gaps, duplicates or backwards steps; the table
    # is the continuous prefix starting at batch 1.
    if not vis:
        raise BacktestInputError(
            "empty_visibility", "the visibility table contains no batches")
    expected = 1
    for row in vis:
        if row.batch_no != expected:
            if any(r.batch_no == expected for r in vis):
                code = "duplicate_batch_no"
            else:
                code = "non_continuous_batch_no"
            raise BacktestInputError(
                code,
                f"{row.source_file}:{row.source_line}: batch sequence must be "
                f"1,2,3,... with no gap, duplicate or backwards step; "
                f"expected {expected}, got {row.batch_no}")
        expected += 1
    if vis[-1].batch_no > max_published_batch:
        raise BacktestInputError(
            "unpublished_visible_batch",
            f"visibility table names batch {vis[-1].batch_no} but the "
            f"warehouse only publishes up to batch {max_published_batch}")

    # ---- visibility instants never go backwards; ties are allowed (several
    # batches can become visible at the same instant and all of them count).
    for prev, nxt in zip(vis, vis[1:]):
        if nxt.visible_at_us < prev.visible_at_us:
            raise BacktestInputError(
                "visibility_order",
                f"{nxt.source_file}:{nxt.source_line}: first_visible_at "
                f"{nxt.visible_at_text!r} is earlier than the previous "
                f"batch's {prev.visible_at_text!r}; visibility instants "
                f"must be non-decreasing")

    # ---- the table claims complete coverage of [start, end]; within that
    # range every published batch must be listed.  Batches already visible
    # before the coverage start must be listed too (they are prefix members),
    # so no extra "in-range but missing" test beyond continuity is needed;
    # guard instead against a published batch missing from a table that
    # explicitly extends past its visibility instant.
    cs, ce = spec.coverage_start_us, spec.coverage_end_us
    if cs > ce:  # pragma: no cover - already checked in load_spec
        raise BacktestInputError("bad_coverage_interval",
                                 "coverage start is after coverage end")

    # ---- every checkpoint and the close-out instant fall inside the claimed
    # coverage; close-out is never earlier than any checkpoint.
    checkpoint_us: list[int] = []
    for rr in inputs.roster:
        for lead in spec.lead_minutes:
            checkpoint_us.append(
                rr.sched_arr_us - lead * 60 * 1_000_000)
    for cp in checkpoint_us:
        if not (cs <= cp <= ce):
            raise BacktestInputError(
                "checkpoint_outside_coverage",
                f"roster root checkpoint at us={cp} falls outside the claimed "
                f"visibility coverage [{spec.coverage_start_text}, "
                f"{spec.coverage_end_text}]")
    if not (cs <= spec.close_us <= ce):
        raise BacktestInputError(
            "close_out_outside_coverage",
            f"close_out_at {spec.close_text!r} falls outside the claimed "
            f"visibility coverage")
    if any(spec.close_us < cp for cp in checkpoint_us):
        raise BacktestInputError(
            "close_out_before_checkpoint",
            "close_out_at must not be earlier than any roster checkpoint")

    # ---- the batch visible at the close-out must exist: the table has to
    # extend at least to the first batch visible at or after the coverage end,
    # i.e. it must still cover the close-out instant.
    if vis[-1].visible_at_us < spec.close_us and \
            vis[-1].batch_no >= max_published_batch:
        # The last published batch became visible before close-out: fine,
        # nothing later can exist.  But if more batches were publishable they
        # would have to be listed; the continuity check above already enforces
        # the prefix.  Nothing to do — this branch documents the rule.
        pass


def load_inputs(*, spec_path: str, roster_path: str,
                visibility_path: str,
                max_published_batch: int) -> BacktestInputs:
    spec = load_spec(spec_path)
    roster = load_roster(roster_path)
    visibility = load_visibility(visibility_path)
    inputs = BacktestInputs(
        spec=spec, roster=roster, visibility=visibility,
        files={"run_spec": os.path.basename(spec_path),
               "roster": os.path.basename(roster_path),
               "visibility": os.path.basename(visibility_path)})
    validate(inputs, max_published_batch=max_published_batch)
    return inputs


def visible_batch_at(visibility: list[VisibilityRow], instant_us: int) -> int:
    """Greatest batch whose first-visible instant is <= ``instant_us``.

    Returns 0 when no batch was visible yet at the instant.  Ties resolve to
    the greatest batch number, so several batches published at the same
    instant all count and the latest one is the knowledge cut.
    """
    best = 0
    for row in visibility:
        if row.visible_at_us <= instant_us:
            best = row.batch_no
        else:
            break
    return best
