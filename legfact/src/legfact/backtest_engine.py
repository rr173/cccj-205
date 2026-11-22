"""Offline arrival-prediction backtest engine.

The run answers an audit question on historical data only — it never predicts
new flights and never feeds operational decisions:

> At every frozen checkpoint, what did the duty officer actually see for the
> scheduled arrival and the estimated arrival?  Which flights were only later
> known to have already landed?  On the *same* scoreable samples, which of the
> two predictions was more accurate?

Design invariants (mirroring the warehouse's own rules):

* **Checkpoints are frozen first.**  Each root's checkpoint is its roster
  scheduled arrival minus a lead minute.  Later warehouse schedule changes
  never move a checkpoint or re-group a root; unknown and unscoreable rows are
  retained.
* **Two baselines rebuilt as-of then, answers read separately.**  A checkpoint
  uses only the last batch visible at that instant and only updates whose
  ``effective_at`` is at or before the checkpoint.  Updates already received
  but not yet effective, and updates that only become visible later (even
  retroactively effective), cannot enter the as-of facts.  The actual arrival
  used as the answer is re-read at the close-out visible batch.
* **Warehouse identity and authority semantics, not current bindings.**  Each
  knowledge cut carries its own identity graph; a relation that only closes in
  a later batch cannot complete an earlier binding.  Only the authoritative
  sender projects facts; gaps, explicit clears and conflicts are preserved.
* **No Cartesian product, no per-sample warehouse scan.**  One indexed SQL
  stream per fact field per distinct visible batch walks that batch's rows
  once; checkpoints in the batch are adjudicated while the batch's transient
  store is live, then the store is released.  Work is linear in
  (field rows + samples), never in their product.

Memory architecture (the 60k-root / 240k-checkpoint run stays under 512 MiB):

* exactly one historical identity graph is live at a time (batch-outer loop);
* adjudication happens inside each batch, so cross-batch per-root group objects
  are never retained — only one small compact result record per checkpoint;
* repeated strings (raw values, file/XML locations) are interned once;
* sample rows stream to a sink (NDJSON) instead of accumulating in memory, and
  the summary is accumulated online in small per-(group, lead) cells.
"""
from __future__ import annotations

import json
from array import array
from dataclasses import dataclass, field

from . import identity
from .backtest_config import (BacktestInputError, BacktestInputs,
                              to_us, visible_batch_at)
from .storage import Warehouse
from .timeutil import parse_offset_datetime

# Fact fields the backtest projects.
PRED_FIELDS = ("sched_arr", "est_arr")
ANSWER_FIELD = "act_arr"
ALL_FIELDS = PRED_FIELDS + (ANSWER_FIELD,)

# Field projection states (value/absent/cleared/conflict come straight from
# the warehouse field rule; stale is layered on top by the backtest).
ST_VALUE = "value"
ST_ABSENT = "absent"
ST_CLEARED = "cleared"
ST_CONFLICT = "conflict"
ST_STALE = "stale"

# Compact byte codes for the projection states (stored in ``array('b')``).
_CODE_ABSENT, _CODE_VALUE, _CODE_CLEARED, _CODE_CONFLICT, _CODE_STALE = range(5)
_STATE_CODE = {ST_ABSENT: _CODE_ABSENT, ST_VALUE: _CODE_VALUE,
               ST_CLEARED: _CODE_CLEARED, ST_CONFLICT: _CODE_CONFLICT,
               ST_STALE: _CODE_STALE}
_CODE_STATE = {v: k for k, v in _STATE_CODE.items()}

# Sample classes, assigned in this exact precedence order.
CLS_UNOBSERVABLE = "unobservable"
CLS_KNOWN_LANDED = "known_landed_at_checkpoint"
CLS_CLOSE_UNDETERMINED = "close_out_undetermined"
CLS_LATER_KNOWN_LANDED = "later_known_already_landed"
CLS_TARGET = "target_population"
_CLASSES = (CLS_UNOBSERVABLE, CLS_KNOWN_LANDED, CLS_CLOSE_UNDETERMINED,
            CLS_LATER_KNOWN_LANDED, CLS_TARGET)
_CLASS_CODE = {c: i for i, c in enumerate(_CLASSES)}
_CODE_CLASS = {v: k for k, v in _CLASS_CODE.items()}

# Unobservable sub-reasons.
UNO_NO_BATCH = "no_batch_visible_yet"
UNO_ROOT_UNAVAILABLE = "operating_root_unavailable_at_cutoff"
UNO_AIRPORT_MISMATCH = "initial_destination_not_this_airport"
# Close-out undetermined sub-reasons.
CLO_NO_ROOT = "no_airport_operating_root_at_close_out"
CLO_ACTUAL_UNCERTAIN = "actual_arrival_uncertain"
CLO_AFTER_CLOSE = "actual_arrival_after_close_out"

# Availability byte codes.
_AVAIL_UNAVAILABLE, _AVAIL_MISMATCH, _AVAIL_AVAILABLE = range(3)
_AVAIL_STATE = {_AVAIL_UNAVAILABLE: "unavailable",
                _AVAIL_MISMATCH: "airport_mismatch",
                _AVAIL_AVAILABLE: "available"}

# A non-persistent marker used by the streaming binder.
_MISS = object()


# ------------------------------------------------------------- frozen layout
@dataclass(slots=True)
class FieldView:
    """One projected field at one cutoff, with full provenance."""
    state: str
    value: str | None = None
    evidence: list[dict] = field(default_factory=list)
    winning_effective_at: str | None = None
    winning_source_revision: int | None = None


@dataclass(slots=True)
class SampleRow:
    seq: int
    root_id: str
    group: str
    lead_minutes: int
    checkpoint_text: str
    checkpoint_us: int
    visible_batch: int
    close_batch: int
    roster_sched_arr: str
    classification: str
    unobservable_reason: str | None
    close_undetermined_reason: str | None
    checkpoint_binding: str
    close_binding: str
    sched_view: FieldView
    est_view: FieldView
    checkpoint_act_view: FieldView
    close_act_view: FieldView
    answer_act_value: str | None
    answer_act_us: int | None
    sched_error_us: int | None
    est_error_us: int | None
    source_file: str
    source_line: int
    record_id: str


# ------------------------------------------------------------- interning
class _Intern:
    """Maps a repeated string to a stable small integer id (one copy kept)."""

    __slots__ = ("by", "items")

    def __init__(self):
        self.by: dict[str, int] = {}
        self.items: list[str] = []

    def id(self, s: str) -> int:
        idx = self.by.get(s)
        if idx is None:
            idx = len(self.items)
            self.by[s] = idx
            self.items.append(s)
        return idx

    def get(self, idx: int) -> str:
        return self.items[idx]


class _EvidenceTable:
    """Interns full provenance as integer-id tuples over ONE shared intern.

    One record is the compact tuple
    ``(value_id, event_id, batch, xml_loc_id, line, col, sender_id, rev,
       effective_at_id, raw_ref_id)``; every distinct text component is stored
    once in a single shared string intern instead of six parallel tables.
    """

    __slots__ = ("records", "by_key", "strings")

    def __init__(self):
        self.records: list[tuple] = []
        # dedup on the warehouse's natural message identity (event, batch):
        # a short 2-tuple key instead of a full 10-field provenance tuple.
        self.by_key: dict[tuple, int] = {}
        self.strings = _Intern()

    def intern(self, r) -> int:
        s = self.strings
        key = (r["event_id"], r["batch_id"])
        idx = self.by_key.get(key)
        if idx is not None:
            return idx
        value_id = -1 if r["value"] is None else s.id(r["value"])
        idx = len(self.records)
        self.by_key[key] = idx
        self.records.append((
            value_id, s.id(r["event_id"]), r["batch_id"],
            s.id(r["xml_location"]), r["line"], r["columnn"],
            s.id(r["sender"]), r["source_revision"],
            s.id(r["effective_at"]), s.id(r["raw_ref"])))
        return idx

    def as_evidence_dict(self, idx: int) -> dict:
        s = self.strings
        (value_id, event_id, batch, xml_id, line, col, sender_id, rev,
         eff_id, raw_id) = self.records[idx]
        return {
            "event_id": s.get(event_id),
            "receipt_batch": batch,
            "raw_value": None if value_id < 0 else s.get(value_id),
            "xml_location": s.get(xml_id),
            "line": line, "column": col,
            "sender": s.get(sender_id),
            "source_revision": rev,
            "effective_at": s.get(eff_id),
            "raw_file": s.get(raw_id),
        }


# ------------------------------------------------------------- stream core
class _RawGroup:
    """One winning-priority group holding raw rows (evidence interned lazily).

    The store is transient per batch, so raw sqlite Row references cost
    nothing beyond the batch; provenance is interned into the long-lived
    evidence table only when a group is actually selected as a checkpoint's
    eligible winner — never for superseded or never-referenced rows.
    """

    __slots__ = ("eff_us", "rev", "rows")

    def __init__(self, eff_us: int, rev: int, row):
        self.eff_us = eff_us
        self.rev = rev
        # distinct (state, value) -> first raw row; >1 entry = conflict
        self.rows: dict[tuple, object] = {
            (row["state"], row["value"]): row}


class _BucketStore:
    """Per (root, field) ordered groups for one batch replay, then discarded.

    Rows arrive in ascending ``(effective_at, source_revision)`` order.  A
    strictly higher-priority row starts a new group; tied rows accumulate
    distinct payloads so a same-priority conflict is never silently resolved.
    """

    __slots__ = ("buckets", "roots")

    def __init__(self, roots: set[str]):
        self.roots = roots
        self.buckets: dict[tuple[str, str], list[_RawGroup]] = {}

    def add(self, root_id: str, field_name: str, eff_us: int, rev: int,
            row) -> None:
        if root_id not in self.roots:
            return
        key = (root_id, field_name)
        payload = (row["state"], row["value"])
        groups = self.buckets.get(key)
        if groups is None:
            self.buckets[key] = [_RawGroup(eff_us, rev, row)]
            return
        top = groups[-1]
        if eff_us > top.eff_us or (eff_us == top.eff_us and rev > top.rev):
            groups.append(_RawGroup(eff_us, rev, row))
        elif eff_us == top.eff_us and rev == top.rev:
            top.rows.setdefault(payload, row)

    def eligible(self, key: tuple[str, str], checkpoint_us: int):
        groups = self.buckets.get(key)
        if not groups:
            return None
        for grp in reversed(groups):
            if grp.eff_us <= checkpoint_us:
                return grp
        return None

    def latest(self, key: tuple[str, str]):
        groups = self.buckets.get(key)
        return groups[-1] if groups else None



def _stream_field(wh: Warehouse, conn, field_name: str, max_batch: int,
                  ceiling_us: int | None):
    """One indexed stream of one fact field up to a knowledge cut.

    When ``ceiling_us`` is ``None`` every effective instant is streamed (the
    close-out answer must keep future-dated actuals); otherwise the ceiling is
    rounded up to the next whole second because warehouse effective times are
    whole-second ISO strings.
    """
    if ceiling_us is None:
        return conn.execute(
            "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
            "e.effective_at, e.identity_at, e.raw_ref, "
            "k.carrier, k.flight_no, k.suffix, k.service_date, "
            "k.departure, k.arrival, "
            "f.state, f.value, f.xml_location, f.line, f.columnn "
            "FROM event_fields f "
            "JOIN events e ON e.event_id=f.event_id AND e.batch_id=f.batch_id "
            "JOIN event_keys k ON k.event_id=e.event_id AND k.batch_id=e.batch_id "
            "WHERE f.field=? AND e.batch_id<=? "
            "ORDER BY e.batch_id, e.effective_at, e.source_revision, "
            "e.event_id, e.batch_id",
            (field_name, max_batch))
    from .landing import _ceil_second, _iso_z
    ceiling = _iso_z(_ceil_second(_utc_dt(ceiling_us)))
    return conn.execute(
        "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
        "e.effective_at, e.identity_at, e.raw_ref, "
        "k.carrier, k.flight_no, k.suffix, k.service_date, "
        "k.departure, k.arrival, "
        "f.state, f.value, f.xml_location, f.line, f.columnn "
        "FROM event_fields f "
        "JOIN events e ON e.event_id=f.event_id AND e.batch_id=f.batch_id "
        "JOIN event_keys k ON k.event_id=e.event_id AND k.batch_id=e.batch_id "
        "WHERE f.field=? AND e.batch_id<=? AND e.effective_at<=? "
        "ORDER BY e.batch_id, e.effective_at, e.source_revision, "
        "e.event_id, e.batch_id",
        (field_name, max_batch, ceiling))


def _utc_dt(instant_us: int):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(instant_us / 1_000_000, tz=timezone.utc)


def _us_to_iso(instant_us: int) -> str:
    return _utc_dt(instant_us).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# =============================================================== sinks
class RowSink:
    def write(self, row: SampleRow) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ListSink(RowSink):
    def __init__(self) -> None:
        self.rows: list[SampleRow] = []

    def write(self, row: SampleRow) -> None:
        self.rows.append(row)


class NdjsonFileSink(RowSink):
    """Streams each serialized sample row straight to disk, one JSON/line."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")
        self.count = 0

    def write(self, row: SampleRow) -> None:
        self._fh.write(json.dumps(row_to_dict(row), ensure_ascii=False,
                                  sort_keys=True) + "\n")
        self.count += 1

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "NdjsonFileSink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ========================================================= compact columns
class _FactColumns:
    """One compact result per checkpoint for one fact field, columnar.

    All arrays are pre-sized to the checkpoint count and hold tiny integer
    codes/ids (one byte states, 8-byte instants, 4-byte evidence ids), so a
    240k-checkpoint run keeps a handful of arrays rather than nested per-root
    dicts of Python objects.
    """

    __slots__ = ("state", "value", "eff_us", "rev", "ev1", "ev2")

    def __init__(self, n: int):
        self.state = array("b", [-1]) * n
        self.value = array("i", [-1]) * n       # interned value id
        self.eff_us = array("q", [0]) * n       # winning effective instant
        self.rev = array("i", [0]) * n
        self.ev1 = array("i", [-1]) * n         # winner / first contender
        self.ev2 = array("i", [-1]) * n         # second contender (conflict)

    def put(self, i: int, state_code: int, value_id: int, eff_us: int,
            rev: int, ev_ids: list[int]) -> None:
        self.state[i] = state_code
        self.value[i] = value_id
        self.eff_us[i] = eff_us
        self.rev[i] = rev
        self.ev1[i] = ev_ids[0]
        if len(ev_ids) > 1:
            self.ev2[i] = ev_ids[1]


def _adjudicate_to_columns(group: "_RawGroup | None", seq: int,
                           checkpoint_us: int, col: _FactColumns,
                           evidence: _EvidenceTable,
                           stale_allowed: bool) -> None:
    """Adjudicate an eligible raw group into compact checkpoint cell ``seq``.

    Provenance rows are interned HERE — only for the group that actually wins
    this checkpoint — so the long-lived evidence table never accumulates the
    many superseded messages that flow through the transient batch store.
    """
    i = seq
    if group is None:
        col.state[i] = _CODE_ABSENT
        return
    strings = evidence.strings
    # intern evidence lazily and map each distinct payload to its evidence id
    payload_ev: dict[tuple, int] = {}
    for payload, raw in group.rows.items():
        payload_ev[payload] = evidence.intern(raw)
    if len(payload_ev) > 1:
        ev_ids = sorted(payload_ev.values())[:2]
        col.put(i, _CODE_CONFLICT, -1, group.eff_us, group.rev, ev_ids)
        return
    (state, value), ev_id = next(iter(payload_ev.items()))
    value_id = -1 if value is None else strings.id(value)
    if state == "cleared":
        col.put(i, _CODE_CLEARED, -1, group.eff_us, group.rev, [ev_id])
        return
    code = _CODE_VALUE
    if stale_allowed and group.eff_us <= checkpoint_us:
        code = _CODE_STALE
    col.put(i, code, value_id, group.eff_us, group.rev, [ev_id])

def _view_from_columns(col: _FactColumns, i: int, evidence: _EvidenceTable,
                       values: _Intern) -> FieldView:
    """Reconstruct a full provenance FieldView from the compact cells."""
    code = col.state[i]
    if code < 0 or code == _CODE_ABSENT:
        return FieldView(ST_ABSENT)
    state = _CODE_STATE[code]
    ev_ids = [col.ev1[i]]
    if col.ev2[i] >= 0:
        ev_ids.append(col.ev2[i])
    ev = [evidence.as_evidence_dict(x) for x in ev_ids if x >= 0]
    value = None
    if col.value[i] >= 0:
        value = values.get(col.value[i])
    eff_text = _us_to_iso(col.eff_us[i]) if col.eff_us[i] else None
    return FieldView(state, value=value, evidence=ev,
                     winning_effective_at=eff_text,
                     winning_source_revision=int(col.rev[i]) or None)


# =============================================================== binding
def _bind_row(graph: identity.IdentityGraph, row,
              bind_cache: dict[bytes, str | None]) -> str | None:
    sender = row["sender"]
    bind_key = (row["carrier"] + "\x1f" + row["flight_no"] + "\x1f"
                + row["suffix"] + "\x1f" + row["service_date"] + "\x1f"
                + row["departure"] + "\x1f" + row["arrival"] + "\x1f"
                + row["identity_at"] + "\x1f" + sender).encode()
    root = bind_cache.get(bind_key, _MISS)
    if root is not _MISS:
        return root
    key = identity.OpKey(
        row["carrier"], row["flight_no"], row["suffix"],
        row["service_date"], row["departure"], row["arrival"], "OP")
    b = graph.bind_operating(key, parse_offset_datetime(row["identity_at"]))
    root = (b.root_id if b.status == "bound" and b.sender == sender else None)
    bind_cache[bind_key] = root
    return root


def _availability_code(graph: identity.IdentityGraph, root_id: str,
                       airport: str) -> int:
    il = graph.initials.get(root_id)
    if il is None:
        return _AVAIL_UNAVAILABLE
    if il.key.arrival != airport:
        return _AVAIL_MISMATCH
    return _AVAIL_AVAILABLE


def _classify(*, visible_batch: int, checkpoint_us: int, close_us: int,
              cp_avail: int, close_avail: int, cp_act_code: int,
              cp_act_value_us: int | None, close_act_code: int,
              close_act_value_us: int | None):
    """Assign (class code, unobservable reason, close reason) in fixed order."""
    if visible_batch == 0:
        return _CLASS_CODE[CLS_UNOBSERVABLE], UNO_NO_BATCH, None
    if cp_avail == _AVAIL_UNAVAILABLE:
        return _CLASS_CODE[CLS_UNOBSERVABLE], UNO_ROOT_UNAVAILABLE, None
    if cp_avail == _AVAIL_MISMATCH:
        return _CLASS_CODE[CLS_UNOBSERVABLE], UNO_AIRPORT_MISMATCH, None
    # known landed at the checkpoint
    if cp_act_code in (_CODE_VALUE, _CODE_STALE) and cp_act_value_us is not None \
            and cp_act_value_us <= checkpoint_us:
        return _CLASS_CODE[CLS_KNOWN_LANDED], None, None
    # close-out undetermined, with the precise sub-reason
    if close_avail in (_AVAIL_UNAVAILABLE, _AVAIL_MISMATCH):
        return _CLASS_CODE[CLS_CLOSE_UNDETERMINED], None, CLO_NO_ROOT
    if close_act_code in (_CODE_ABSENT, _CODE_CLEARED, _CODE_CONFLICT) \
            or close_act_code < 0:
        return _CLASS_CODE[CLS_CLOSE_UNDETERMINED], None, CLO_ACTUAL_UNCERTAIN
    if close_act_value_us is not None and close_act_value_us > close_us:
        return _CLASS_CODE[CLS_CLOSE_UNDETERMINED], None, CLO_AFTER_CLOSE
    # later known to have already landed at the checkpoint
    if close_act_value_us is not None \
            and close_act_value_us <= checkpoint_us:
        return _CLASS_CODE[CLS_LATER_KNOWN_LANDED], None, None
    return _CLASS_CODE[CLS_TARGET], None, None


# =============================================================== the runs
def run_backtest(wh: Warehouse, inputs: BacktestInputs,
                 stats: dict | None = None) -> dict:
    """In-memory convenience wrapper around the streaming backtest."""
    sink = ListSink()
    result = run_backtest_streaming(wh, inputs, sink, stats)
    result["rows"] = sink.rows
    return result


def run_backtest_streaming(wh: Warehouse, inputs: BacktestInputs,
                           sink: RowSink, stats: dict | None = None) -> dict:
    """Memory-bounded backtest; rows stream to ``sink``, summary is online."""
    stats = stats if stats is not None else {}
    spec = inputs.spec
    conn = wh._conn
    if not wh.published_batches():
        raise BacktestInputError(
            "no_published_batch", "the warehouse has no published batch")
    # ---- freeze checkpoints from the roster only, stored as compact arrays
    # indexed by global checkpoint sequence (no per-checkpoint objects).  All
    # repeated strings (root id, group, roster time, source file, record id)
    # are interned into small integer ids so a 240k-row run keeps arrays, not
    # 240k boxed string references.
    cp_intern = _Intern()
    cp_root_id = array("i")
    cp_group_id = array("i")
    cp_lead = array("i")
    cp_us_arr = array("q")
    cp_vis_batch = array("i")
    cp_roster_text_id = array("i")
    cp_src_file_id = array("i")
    cp_src_line = array("i")
    cp_record_id = array("i")
    # visible batch -> checkpoint sequences (compact integer lists)
    by_batch: dict[int, list[int]] = {}
    seq = 0
    for rr in inputs.roster:
        rid = cp_intern.id(rr.root_id)
        gid = cp_intern.id(rr.group)
        rtid = cp_intern.id(rr.sched_arr_text)
        sfid = cp_intern.id(rr.source_file)
        rcid = cp_intern.id(rr.record_id)
        for lead in spec.lead_minutes:
            cp_us = rr.sched_arr_us - lead * 60 * 1_000_000
            vb = visible_batch_at(inputs.visibility, cp_us)
            cp_root_id.append(rid)
            cp_group_id.append(gid)
            cp_lead.append(lead)
            cp_us_arr.append(cp_us)
            cp_vis_batch.append(vb)
            cp_roster_text_id.append(rtid)
            cp_src_file_id.append(sfid)
            cp_src_line.append(rr.source_line)
            cp_record_id.append(rcid)
            if vb > 0:
                by_batch.setdefault(vb, []).append(seq)
            seq += 1
    n = seq
    close_batch = visible_batch_at(inputs.visibility, spec.close_us)
    if close_batch == 0:
        raise BacktestInputError(
            "no_batch_visible_at_close_out",
            "no visibility row is at or before the close-out instant")
    for b in by_batch:
        wh.require_published(b)
    wh.require_published(close_batch)

    roster_roots = {rr.root_id for rr in inputs.roster}

    # ---- compact result columns, one set per fact field
    sched_col = _FactColumns(n)
    est_col = _FactColumns(n)
    cp_act_col = _FactColumns(n)
    cp_avail = array("b", [-1]) * n
    close_avail_arr = array("b", [-1]) * len(inputs.roster)
    close_act_col = _FactColumns(len(inputs.roster))

    evidence = _EvidenceTable()
    values = evidence.strings

    stats["sql_statements"] = 0
    stats["field_rows_scanned"] = 0
    stats["distinct_bind_candidates"] = 0

    # ---- batch-outer loop: one live identity graph; adjudicate inside batch
    field_cols = {"sched_arr": (sched_col, True),
                  "est_arr": (est_col, True),
                  "act_arr": (cp_act_col, False)}
    for b in sorted(by_batch):
        graph = wh.load_graph(b)
        seqs = by_batch[b]
        seq_root = {i: cp_intern.get(cp_root_id[i]) for i in seqs}
        batch_roots = set(seq_root.values())
        max_cp_us = max(cp_us_arr[i] for i in seqs)
        for i in seqs:
            cp_avail[i] = _availability_code(
                graph, seq_root[i], spec.airport)
        for field_name in ALL_FIELDS:
            col, stale_allowed = field_cols[field_name]
            store = _BucketStore(batch_roots)
            bind_cache: dict[bytes, str | None] = {}
            cur = _stream_field(wh, conn, field_name, b, max_cp_us)
            stats["sql_statements"] += 1
            for r in cur:
                stats["field_rows_scanned"] += 1
                eff_us = to_us(parse_offset_datetime(r["effective_at"]))
                if eff_us > max_cp_us:
                    continue
                root = _bind_row(graph, r, bind_cache)
                if root is None:
                    continue
                store.add(root, field_name, eff_us,
                          r["source_revision"], r)
            stats["distinct_bind_candidates"] = max(
                stats["distinct_bind_candidates"], len(bind_cache))
            for i in seqs:
                grp = store.eligible((seq_root[i], field_name),
                                     cp_us_arr[i])
                _adjudicate_to_columns(grp, i, cp_us_arr[i], col, evidence,
                                       stale_allowed)
            del store, bind_cache
        del graph

    # ---- close-out answer stream (no effective ceiling), own graph
    close_graph = wh.load_graph(close_batch)
    close_store = _BucketStore(roster_roots)
    close_bind_cache: dict[bytes, str | None] = {}
    cur = _stream_field(wh, conn, ANSWER_FIELD, close_batch, None)
    stats["sql_statements"] += 1
    for r in cur:
        stats["field_rows_scanned"] += 1
        eff_us = to_us(parse_offset_datetime(r["effective_at"]))
        root = _bind_row(close_graph, r, close_bind_cache)
        if root is None:
            continue
        close_store.add(root, ANSWER_FIELD, eff_us,
                        r["source_revision"], r)
    stats["distinct_bind_candidates"] = max(
        stats["distinct_bind_candidates"], len(close_bind_cache))
    root_pos = {rr.root_id: i for i, rr in enumerate(inputs.roster)}
    for rr in inputs.roster:
        pos = root_pos[rr.root_id]
        close_avail_arr[pos] = _availability_code(
            close_graph, rr.root_id, spec.airport)
        # latest group, no effectiveness ceiling (future actuals must survive)
        grp = close_store.latest((rr.root_id, ANSWER_FIELD))
        _adjudicate_to_columns(grp, pos, spec.close_us, close_act_col,
                               evidence, False)
    del close_store, close_graph, close_bind_cache

    # ---- resolve each checkpoint and emit immediately
    agg = _OnlineAggregator(sorted({rr.group for rr in inputs.roster}),
                            list(spec.lead_minutes))
    for i in range(n):
        root = cp_intern.get(cp_root_id[i])
        pos = root_pos[root]
        sv = _view_from_columns(sched_col, i, evidence, values)
        ev_view = _view_from_columns(est_col, i, evidence, values)
        cp_act = _view_from_columns(cp_act_col, i, evidence, values)
        cl_act = _view_from_columns(close_act_col, pos, evidence, values)
        cp_avail_code = cp_avail[i]
        close_avail_code = close_avail_arr[pos]

        def _value_us(view: FieldView):
            return (to_us(parse_offset_datetime(view.value))
                    if view.value is not None else None)

        cls_code, uno, clo = _classify(
            visible_batch=cp_vis_batch[i], checkpoint_us=cp_us_arr[i],
            close_us=spec.close_us,
            cp_avail=cp_avail_code, close_avail=close_avail_code,
            cp_act_code=cp_act_col.state[i],
            cp_act_value_us=_value_us(cp_act),
            close_act_code=close_act_col.state[pos],
            close_act_value_us=_value_us(cl_act))
        cls = _CODE_CLASS[cls_code]

        answer_value = answer_us = None
        sched_err = est_err = None
        if cls == CLS_TARGET and cl_act.value is not None:
            answer_value = cl_act.value
            answer_us = to_us(parse_offset_datetime(answer_value))
            if sv.value is not None:
                sched_err = to_us(parse_offset_datetime(sv.value)) - answer_us
            if ev_view.value is not None:
                est_err = to_us(parse_offset_datetime(ev_view.value)) - answer_us

        row = SampleRow(
            seq=i, root_id=root,
            group=cp_intern.get(cp_group_id[i]),
            lead_minutes=cp_lead[i],
            checkpoint_text=_us_to_iso(cp_us_arr[i]),
            checkpoint_us=cp_us_arr[i],
            visible_batch=cp_vis_batch[i], close_batch=close_batch,
            roster_sched_arr=cp_intern.get(cp_roster_text_id[i]),
            classification=cls,
            unobservable_reason=uno, close_undetermined_reason=clo,
            checkpoint_binding=("no_batch" if cp_vis_batch[i] == 0
                                else _AVAIL_STATE[cp_avail_code]),
            close_binding=_AVAIL_STATE[close_avail_code],
            sched_view=sv, est_view=ev_view,
            checkpoint_act_view=cp_act, close_act_view=cl_act,
            answer_act_value=answer_value, answer_act_us=answer_us,
            sched_error_us=sched_err, est_error_us=est_err,
            source_file=cp_intern.get(cp_src_file_id[i]),
            source_line=cp_src_line[i],
            record_id=cp_intern.get(cp_record_id[i]))
        sink.write(row)
        agg.add(row)

    return {"summary": agg.build(), "close_batch": close_batch,
            "stats": stats}


# ========================================================= online summary
class _OnlineAggregator:
    """Accumulates summary denominators/metrics from streamed rows only."""

    __slots__ = ("cells", "groups", "leads", "total_points", "class_counts")

    def __init__(self, groups: list[str], leads: list[int]):
        self.groups = groups
        self.leads = leads
        self.cells: dict[tuple, dict] = {}
        self.total_points = 0
        self.class_counts = {c: 0 for c in _CLASSES}

    def _cell(self, group: str, lead: int) -> dict:
        key = (group, lead)
        cell = self.cells.get(key)
        if cell is None:
            # integer arrays, not Python int lists, so a 60k-root run keeps
            # 8 bytes per error instead of ~32 bytes per boxed integer
            cell = {
                "roster_points": 0,
                "exclusions": {CLS_UNOBSERVABLE: 0, CLS_KNOWN_LANDED: 0,
                               CLS_CLOSE_UNDETERMINED: 0,
                               CLS_LATER_KNOWN_LANDED: 0},
                "target_population": 0,
                "sched_err": array("q"), "est_err": array("q"),
                "paired_sched": array("q"), "paired_est": array("q"),
            }
            self.cells[key] = cell
        return cell

    def add(self, row: SampleRow) -> None:
        cell = self._cell(row.group, row.lead_minutes)
        cell["roster_points"] += 1
        self.total_points += 1
        self.class_counts[row.classification] += 1
        if row.classification in cell["exclusions"]:
            cell["exclusions"][row.classification] += 1
            return
        cell["target_population"] += 1
        if row.sched_error_us is not None:
            cell["sched_err"].append(row.sched_error_us)
        if row.est_error_us is not None:
            cell["est_err"].append(row.est_error_us)
        if row.sched_error_us is not None and row.est_error_us is not None:
            cell["paired_sched"].append(row.sched_error_us)
            cell["paired_est"].append(row.est_error_us)

    def build(self) -> dict:
        grouped: list[dict] = []
        for g in self.groups:
            for lead in self.leads:
                grouped.append({"group": g, "lead_minutes": lead,
                                **self._block(self.cells.get((g, lead)))})
        overall: list[dict] = []
        for lead in self.leads:
            overall.append({"scope": "all_groups", "lead_minutes": lead,
                            **self._merge(
                                [self.cells.get((g, lead))
                                 for g in self.groups])})
        return {"per_group_lead": grouped, "overall_per_lead": overall,
                "conservation": self._conservation(),
                "groups": self.groups, "lead_minutes": self.leads}

    def _merge(self, cells: list[dict | None]) -> dict:
        merged = {
            "roster_points": 0,
            "exclusions": {CLS_UNOBSERVABLE: 0, CLS_KNOWN_LANDED: 0,
                           CLS_CLOSE_UNDETERMINED: 0,
                           CLS_LATER_KNOWN_LANDED: 0},
            "target_population": 0,
            "sched_err": array("q"), "est_err": array("q"),
            "paired_sched": array("q"), "paired_est": array("q"),
        }
        for cell in cells:
            if cell is None:
                continue
            merged["roster_points"] += cell["roster_points"]
            for k in merged["exclusions"]:
                merged["exclusions"][k] += cell["exclusions"][k]
            merged["target_population"] += cell["target_population"]
            merged["sched_err"].extend(cell["sched_err"])
            merged["est_err"].extend(cell["est_err"])
            merged["paired_sched"].extend(cell["paired_sched"])
            merged["paired_est"].extend(cell["paired_est"])
        return self._block(merged)

    @staticmethod
    def _block(cell: dict | None) -> dict:
        if cell is None:
            cell = {"roster_points": 0,
                    "exclusions": {CLS_UNOBSERVABLE: 0, CLS_KNOWN_LANDED: 0,
                                   CLS_CLOSE_UNDETERMINED: 0,
                                   CLS_LATER_KNOWN_LANDED: 0},
                    "target_population": 0,
                    "sched_err": array("q"), "est_err": array("q"),
                    "paired_sched": array("q"), "paired_est": array("q")}
        sched_err = cell["sched_err"]
        est_err = cell["est_err"]
        paired_sched = cell["paired_sched"]
        paired_est = cell["paired_est"]
        paired_diff = sum(abs(e) - abs(s)
                          for s, e in zip(paired_sched, paired_est))
        return {
            "roster_points": cell["roster_points"],
            "exclusions": dict(cell["exclusions"]),
            "target_population": cell["target_population"],
            "sched_coverage_n": len(sched_err),
            "est_coverage_n": len(est_err),
            "both_available_n": len(paired_sched),
            "sched_one_sided": _metric_block(sched_err),
            "est_one_sided": _metric_block(est_err),
            "paired": None if not len(paired_sched) else {
                "n": len(paired_sched),
                "sched": _metric_block(paired_sched),
                "est": _metric_block(paired_est),
                "paired_abs_diff_est_minus_sched_us": paired_diff,
            },
        }

    def _conservation(self) -> dict:
        checks = {
            "classes_partition_all_rows":
                sum(self.class_counts.values()) == self.total_points,
            "grouped_points_equal_rows":
                sum(c["roster_points"] for c in self.cells.values())
                == self.total_points,
            "overall_points_equal_rows":
                sum(c["roster_points"] for c in self.cells.values())
                == self.total_points,
            "coverage_bounds_consistent": all(
                c["target_population"] >= len(c["sched_err"])
                and c["target_population"] >= len(c["est_err"])
                and min(len(c["sched_err"]), len(c["est_err"]))
                >= len(c["paired_sched"])
                for c in self.cells.values()),
        }
        checks["all_conservation_ok"] = all(checks.values())
        return {"checks": checks, "class_counts": dict(self.class_counts),
                "total_rows": self.total_points}


def _percentile_90(values: list[int]) -> int:
    import math
    return sorted(values)[math.ceil(0.9 * len(values)) - 1]


def _metric_block(errors: list[int]) -> dict | None:
    if not errors:
        return None
    abs_errors = [abs(e) for e in errors]
    return {
        "n": len(errors),
        "signed_error_sum_us": sum(errors),
        "abs_error_sum_us": sum(abs_errors),
        "abs_error_p90_us": _percentile_90(abs_errors),
    }


# ------------------------------------------------------------- serialization
def _view_to_dict(v: FieldView) -> dict:
    return {
        "state": v.state,
        "value": v.value,
        "winning_effective_at": v.winning_effective_at,
        "winning_source_revision": v.winning_source_revision,
        "evidence": v.evidence,
    }


def row_to_dict(r: SampleRow) -> dict:
    return {
        "seq": r.seq, "root_id": r.root_id, "group": r.group,
        "lead_minutes": r.lead_minutes,
        "checkpoint_at": r.checkpoint_text,
        "checkpoint_us": r.checkpoint_us,
        "visible_batch": r.visible_batch, "close_out_batch": r.close_batch,
        "roster_sched_arr": r.roster_sched_arr,
        "classification": r.classification,
        "unobservable_reason": r.unobservable_reason,
        "close_undetermined_reason": r.close_undetermined_reason,
        "checkpoint_binding": r.checkpoint_binding,
        "close_out_binding": r.close_binding,
        "sched_arr": _view_to_dict(r.sched_view),
        "est_arr": _view_to_dict(r.est_view),
        "checkpoint_act_arr": _view_to_dict(r.checkpoint_act_view),
        "close_out_act_arr": _view_to_dict(r.close_act_view),
        "answer_act_arr": r.answer_act_value,
        "answer_act_arr_us": r.answer_act_us,
        "sched_error_us": r.sched_error_us,
        "est_error_us": r.est_error_us,
        "roster_source": {"file": r.source_file, "line": r.source_line,
                          "record_id": r.record_id},
    }
