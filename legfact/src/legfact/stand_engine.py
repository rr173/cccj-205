"""Offline stand-occupancy reconciliation engine.

The output of one run is a **time ledger cut by non-repeatable physical
units**: for every physical unit, a gap-free, non-overlapping partition of the
whole audit window.  Each segment carries an independent measured state (what
the sensors saw) and an independent ledger attribution (which turnaround the
usage ledger claims the ground for), plus the reconciliation class derived
from the two.

Design notes
------------
* Everything is compared on absolute UTC instants stored as exact integer
  microseconds.  Original offset-bearing text is retained for evidence.
* Ledger claims are never summed on logical stand names: each claim is
  translated to physical units through the map version valid at each part of
  its interval, and conflicts are resolved per physical unit-time.
* Sensor state is swept per unit from point observations and online intervals;
  state never leaks across an online-interval right end.
* All sweep events of one unit are merged before emitting, so simultaneous
  ends/starts/remaps/observations never create zero-length intermediate
  segments.
"""
from __future__ import annotations

import os
import gc
from dataclasses import dataclass, field

from .stand_config import (OBS_IDLE, OBS_OCCUPIED, LedgerRecord, MapVersion,
                           StandInputError, StandInputs, to_us)
from .stand_facts import (R_ACTUAL_UNCERTAIN, R_AFTER_CUTOFF,
                          R_AIRPORT_MISMATCH, R_DEP_NOT_AFTER_ARR,
                          R_UNKNOWN_ROOT, adjudicate, evidence_of, scan_actuals)
from .storage import Warehouse
from .timeutil import parse_offset_datetime

# Measured states emitted on ledger segments.
MS_OCCUPIED = "occupied"
MS_IDLE = "idle"
MS_UNKNOWN = "unknown"

# Unknown-state reasons.
UR_OFFLINE = "offline"
UR_WAITING_FIRST = "waiting_first_observation"
UR_CONFLICT = "observation_conflict"

# Attribution states.
AT_NONE = "none"
AT_UNIQUE = "unique_visit"
AT_CONFLICT = "conflict"

# Reconciliation classes.
CLS_MATCH_OCCUPIED = "match_occupied"          # both say occupied
CLS_MATCH_IDLE = "match_idle"                  # both say free
CLS_MISMATCH = "mismatch"                      # unique claim, sensor idle
CLS_UNATTRIBUTED = "unattributed_occupancy"    # sensor occupied, no claim
CLS_PENDING = "pending_review"                 # attribution decided, sensor unknown
CLS_ATTR_CONFLICT = "attribution_conflict"     # claims conflict; match not judged

ALL_CLASSES = (CLS_MATCH_OCCUPIED, CLS_MATCH_IDLE, CLS_MISMATCH,
               CLS_UNATTRIBUTED, CLS_PENDING, CLS_ATTR_CONFLICT)

# Issue kinds for inputs that could not participate.
I_UNAVAILABLE_VISIT = "unavailable_visit"
I_UNKNOWN_VISIT_LEDGER = "ledger_unknown_visit"
I_UNAVAILABLE_VISIT_LEDGER = "ledger_unavailable_visit"
I_DISJOINT_LEDGER = "ledger_disjoint_from_ground"
I_MAPPING_GAP = "logical_stand_unmapped"
I_OFFLINE_OBSERVATION = "offline_observation"

SCHEMA = """
CREATE TABLE recon_sources (
  source_id   INTEGER PRIMARY KEY,
  file        TEXT NOT NULL,
  line        INTEGER NOT NULL,
  record_id   TEXT NOT NULL
);
CREATE TABLE recon_runs (
  run_id            INTEGER PRIMARY KEY CHECK (run_id = 1),
  airport           TEXT NOT NULL,
  window_start_utc  TEXT NOT NULL,
  window_end_utc    TEXT NOT NULL,
  window_start_us   INTEGER NOT NULL,
  window_end_us     INTEGER NOT NULL,
  window_us         INTEGER NOT NULL,
  receipt_batch     INTEGER NOT NULL,
  cutoff_text       TEXT NOT NULL,
  cutoff_utc        TEXT NOT NULL,
  inputs_json       TEXT NOT NULL,
  created_at        TEXT NOT NULL
);
CREATE TABLE recon_visits (
  visit_id          TEXT PRIMARY KEY,
  status            TEXT NOT NULL,
  arrival_root      TEXT NOT NULL,
  departure_root    TEXT NOT NULL,
  ground_start_us   INTEGER,
  ground_end_us     INTEGER,
  reason            TEXT,
  reason_detail     TEXT,
  source_id         INTEGER NOT NULL,
  arr_evidence_json TEXT,
  dep_evidence_json TEXT
);
CREATE TABLE recon_ledger (
  record_id         TEXT PRIMARY KEY,
  visit_id          TEXT NOT NULL,
  stand             TEXT NOT NULL,
  start_us          INTEGER NOT NULL,
  end_us            INTEGER NOT NULL,
  source_id         INTEGER NOT NULL,
  usable            INTEGER NOT NULL,
  issue_kind        TEXT
);
CREATE TABLE recon_claims (
  claim_id          INTEGER PRIMARY KEY,
  ledger_record_id  TEXT NOT NULL,
  visit_id          TEXT NOT NULL,
  stand             TEXT NOT NULL,
  unit_id           TEXT NOT NULL,
  start_us          INTEGER NOT NULL,
  end_us            INTEGER NOT NULL,
  map_version       TEXT NOT NULL,
  source_id         INTEGER NOT NULL
);
CREATE INDEX idx_claims_unit ON recon_claims(unit_id, start_us, end_us);
CREATE INDEX idx_claims_visit ON recon_claims(visit_id);
CREATE TABLE recon_online (
  record_id         TEXT PRIMARY KEY,
  unit_id           TEXT NOT NULL,
  start_us          INTEGER NOT NULL,
  end_us            INTEGER NOT NULL,
  source_id         INTEGER NOT NULL
);
CREATE INDEX idx_online_unit ON recon_online(unit_id, start_us);
CREATE TABLE recon_observations (
  record_id         TEXT PRIMARY KEY,
  unit_id           TEXT NOT NULL,
  at_us             INTEGER NOT NULL,
  state             TEXT NOT NULL,
  in_online         INTEGER NOT NULL,
  source_id         INTEGER NOT NULL
);
CREATE INDEX idx_obs_unit ON recon_observations(unit_id, at_us);
CREATE TABLE recon_segments (
  segment_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  unit_id           TEXT NOT NULL,
  start_us          INTEGER NOT NULL,
  end_us            INTEGER NOT NULL,
  duration_us       INTEGER NOT NULL,
  measured_state    TEXT NOT NULL,
  unknown_reason    TEXT,
  attribution       TEXT NOT NULL,
  visit_id          TEXT,
  conflict_visits_json TEXT,
  recon_class       TEXT NOT NULL,
  measured_evidence_json TEXT,
  claim_ids_json    TEXT NOT NULL
);
CREATE INDEX idx_seg_unit ON recon_segments(unit_id, start_us);
CREATE TABLE recon_issues (
  issue_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind              TEXT NOT NULL,
  detail            TEXT NOT NULL,
  source_id         INTEGER NOT NULL,
  visit_id          TEXT,
  ledger_record_id  TEXT,
  unit_id           TEXT
);
CREATE INDEX idx_issues_kind ON recon_issues(kind);
"""


class ReconError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


@dataclass(slots=True)
class Claim:
    claim_id: int
    ledger_record_id: str
    visit_id: str
    stand: str
    unit_id: str
    start: int
    end: int
    map_version: str
    source_id: int


@dataclass(slots=True)
class Issue:
    kind: str
    detail: str
    source_id: int
    visit_id: str | None = None
    ledger_record_id: str | None = None
    unit_id: str | None = None


@dataclass(slots=True)
class VisitOutcome:
    visit_id: str
    status: str                      # determinable | unavailable
    arrival_root: str
    departure_root: str
    ground_start: int | None = None
    ground_end: int | None = None
    reason: str | None = None
    reason_detail: str | None = None
    source_id: int = -1
    arr_evidence: list = field(default_factory=list)
    dep_evidence: list = field(default_factory=list)


# ============================================================ input helpers
def _version_at(versions: list[MapVersion], t_us: int) -> MapVersion:
    """Map version valid at instant t_us (half-open version intervals)."""
    for v in versions:
        if to_us(v.start) <= t_us < to_us(v.end):
            return v
    raise StandInputError("map_coverage_gap",
                          f"no map version covers instant {t_us}")


def split_on_map(lo: int, hi: int, versions: list[MapVersion]):
    """Split an interval on map-version boundaries; yield (lo, hi, version)."""
    bounds = {lo, hi}
    for v in versions:
        vs, ve = to_us(v.start), to_us(v.end)
        if lo < ve and vs < hi:
            if lo < vs < hi:
                bounds.add(vs)
            if lo < ve < hi:
                bounds.add(ve)
    ordered = sorted(bounds)
    for a, b in zip(ordered, ordered[1:]):
        yield a, b, _version_at(versions, a)


# ============================================================== sensor sweep
def build_sensor_timeline(inputs: StandInputs, win_start: int, win_end: int,
                          issues: list[Issue], unit: str) -> list:
    """Measured segments of ONE unit, partitioning [win_start, win_end).

    A segment is ``(start, end, state, unknown_reason, evidence_source_ids)``
    with state in occupied/idle/unknown; the evidence is a list of interned
    source ids.  Offline observations are appended to ``issues`` (offline
    data never produces a state).
    """
    ivs = sorted(((iv.start_us, iv.end_us, iv) for iv in inputs.online
                  if iv.unit_id == unit), key=lambda x: (x[0], x[1]))
    iv_starts = [lo for lo, _hi, _ in ivs]
    blk = inputs.obs_blocks.get(unit)
    if blk is not None:
        import bisect
        for i in range(len(blk.at)):
            at_us = int(blk.at[i])
            packed = int(blk.packed[i])
            sid = packed >> 1
            # logarithmic containment test; online intervals never overlap
            j = bisect.bisect_right(iv_starts, at_us) - 1
            if j < 0 or at_us >= ivs[j][1]:
                try:
                    loc = inputs.sources.resolve(sid)
                    detail = (
                        f"observation {loc['record_id']!r} (source "
                        f"{loc['file']}:{loc['line']}) lies outside every online "
                        f"interval of unit {unit!r}; offline data produces no "
                        f"state")
                except Exception:  # scratch handle already released
                    detail = (
                        f"observation source_id {sid} lies outside every online "
                        f"interval of unit {unit!r}; offline data produces no "
                        f"state")
                issues.append(Issue(I_OFFLINE_OBSERVATION, detail, sid,
                                    unit_id=unit))
    return _sweep_unit(ivs, blk, win_start, win_end)


def _sweep_unit(ivs, blk, win_start, win_end):
    """Sweep one unit directly over its compact observation block.

    ``blk.at`` / ``blk.packed`` are pre-sorted; observations at the same
    instant are a conflict group.  No per-observation dictionary is built,
    which is what keeps the allocator footprint flat at a million rows.
    """
    import bisect
    from array import array
    iv_starts = [lo for lo, _hi, _ in ivs]
    n_obs = 0 if blk is None else len(blk.at)

    # Breakpoints as a sorted, deduplicated raw-int array.  Observation
    # instants are already sorted in the block, so union them with the online
    # boundaries by a linear merge instead of building a million-entry set.
    obs_breaks = array("q")
    if blk is not None:
        last = None
        for i in range(n_obs):
            t = int(blk.at[i])
            if win_start <= t < win_end and t != last:
                obs_breaks.append(t)
                last = t
    iv_breaks = array("q", [win_start])
    for lo, hi, _ in ivs:
        for b in (lo, hi):
            if win_start < b < win_end:
                iv_breaks.append(b)
    iv_sorted = array("q", sorted(iv_breaks))
    breaks = array("q")
    i = j = 0
    while i < len(iv_sorted) or j < len(obs_breaks):
        na = iv_sorted[i] if i < len(iv_sorted) else None
        nb = obs_breaks[j] if j < len(obs_breaks) else None
        if nb is None or (na is not None and na <= nb):
            cand, i = na, i + 1
        else:
            cand, j = nb, j + 1
        if not breaks or cand != breaks[-1]:
            breaks.append(cand)
    if breaks[-1] != win_end:
        breaks.append(win_end)
    del iv_sorted, iv_breaks, obs_breaks

    cur_iv = None            # (lo, hi, record) currently carrying state
    cur_state = None         # occupied | idle | conflict | None
    cur_evidence: list = []  # interned source ids
    # Output segments in parallel compact arrays instead of tuples.
    seg_start_arr = array("q")
    seg_end_arr = array("q")
    seg_state = array("b")
    seg_reason = array("b")
    seg_evidence: list = []
    _st = {MS_IDLE: 0, MS_OCCUPIED: 1, "conflict": 2, MS_UNKNOWN: 3}
    _rs = {None: 0, UR_OFFLINE: 1, UR_WAITING_FIRST: 2, UR_CONFLICT: 3}
    seg_start = win_start
    obs_cursor = 0

    def emit(end, state, reason, evidence):
        nonlocal seg_start
        if end > seg_start:
            seg_start_arr.append(seg_start)
            seg_end_arr.append(end)
            seg_state.append(_st[state])
            seg_reason.append(_rs[reason])
            seg_evidence.append(list(evidence))
        seg_start = end

    def group_at(t):
        """Return (state_code_set, source_ids) for observations exactly at t.

        Advances the shared cursor past every observation before t so the
        cursor always points at the first observation at or after the current
        breakpoint.
        """
        nonlocal obs_cursor
        while obs_cursor < n_obs and int(blk.at[obs_cursor]) < t:
            obs_cursor += 1
        codes, sids = set(), []
        k = obs_cursor
        while k < n_obs and int(blk.at[k]) == t:
            packed = int(blk.packed[k])
            codes.add(packed & 1)
            sids.append(packed >> 1)
            k += 1
        return codes, sids

    def latest_in_interval(iv_lo, iv_hi, before):
        """Latest observation group of this interval at or before ``before``."""
        k = bisect.bisect_right(blk.at, before) - 1
        if k < 0:
            return None
        gt = int(blk.at[k])
        if gt < iv_lo or gt >= iv_hi:
            return None
        lo_i = k
        while lo_i > 0 and int(blk.at[lo_i - 1]) == gt:
            lo_i -= 1
        codes, sids = set(), []
        for q in range(lo_i, k + 1):
            packed = int(blk.packed[q])
            codes.add(packed & 1)
            sids.append(packed >> 1)
        return codes, sids

    for t in breaks:
        if t > seg_start:
            if cur_iv is not None and cur_state is not None:
                if cur_state == "conflict":
                    emit(t, MS_UNKNOWN, UR_CONFLICT, cur_evidence)
                else:
                    emit(t, MS_OCCUPIED if cur_state == OBS_OCCUPIED
                         else MS_IDLE, None, cur_evidence)
            else:
                emit(t, MS_UNKNOWN,
                     UR_WAITING_FIRST if cur_iv is not None else UR_OFFLINE,
                     [])
        if t == win_end:
            break

        j = bisect.bisect_right(iv_starts, t) - 1
        containing = ivs[j] if j >= 0 and t < ivs[j][1] else None

        # advance the cursor to the first observation at/after t, and detect
        # whether one lands exactly on this breakpoint
        while obs_cursor < n_obs and int(blk.at[obs_cursor]) < t:
            obs_cursor += 1
        has_obs = blk is not None and obs_cursor < n_obs \
            and int(blk.at[obs_cursor]) == t
        if has_obs:
            codes, sids = group_at(t)
            if containing is not None:
                cur_iv = containing
                cur_state = "conflict" if len(codes) > 1 else (
                    OBS_OCCUPIED if next(iter(codes)) == 1 else OBS_IDLE)
                cur_evidence = sids
            # offline observations change nothing (issue already recorded)
            continue

        if containing is None:
            cur_iv, cur_state, cur_evidence = None, None, []
            continue

        if containing is not cur_iv:
            # Fresh online interval (incl. one opened before the window): look
            # back for its latest observation at or before t; with none, state
            # stays unknown until this interval's first observation.
            cur_iv = containing
            cur_state, cur_evidence = None, []
            iv_lo, iv_hi, _ = containing
            found = latest_in_interval(iv_lo, iv_hi, t)
            if found is not None:
                codes, sids = found
                cur_state = "conflict" if len(codes) > 1 else (
                    OBS_OCCUPIED if next(iter(codes)) == 1 else OBS_IDLE)
                cur_evidence = sids
        # else: same interval, no new observation -> carry current state.
    return _merge_array_segments(
        seg_start_arr, seg_end_arr, seg_state, seg_reason, seg_evidence)


def _merge_array_segments(starts, ends, states, reasons, evidence):
    """Coalesce adjacent segments with identical state and reason.

    A stable measured state established by one observation is not a new
    segment merely because later observations repeat the same state: adjacent
    same-state/same-reason runs collapse to one segment, keeping the source
    evidence that *established* the state (still fully traceable).  Conflict
    and waiting/unknown regions keep their own reason codes, so they are never
    merged into an idle/occupied run.
    """
    from array import array
    out_starts = array("q")
    out_ends = array("q")
    out_state = array("b")
    out_reason = array("b")
    out_evidence: list = []
    n = len(starts)
    for k in range(n):
        if (out_state and out_ends[-1] == starts[k]
                and out_state[-1] == states[k]
                and out_reason[-1] == reasons[k]):
            # extend the existing run; its establishing evidence is retained
            out_ends[-1] = ends[k]
        else:
            out_starts.append(starts[k])
            out_ends.append(ends[k])
            out_state.append(states[k])
            out_reason.append(reasons[k])
            out_evidence.append(evidence[k])
    code_state = {0: MS_IDLE, 1: MS_OCCUPIED, 2: "conflict", 3: MS_UNKNOWN}
    code_reason = {0: None, 1: UR_OFFLINE, 2: UR_WAITING_FIRST,
                   3: UR_CONFLICT}
    return [(int(out_starts[k]), int(out_ends[k]),
             code_state[int(out_state[k])], code_reason[int(out_reason[k])],
             out_evidence[k]) for k in range(len(out_state))]


# ========================================================== attribution sweep
def build_attribution_for_unit(claims: list[Claim], unit: str,
                               win_start: int, win_end: int) -> list:
    """Attribution segments of ONE unit (see :func:`_attribution_events`).

    Retained for tests and small inputs; the streaming engine uses the
    callback variant below so a hot unit's event/segment objects never pile up.
    """
    out: list = []
    _attribution_events(claims, unit, win_start, win_end, out.append)
    return out


def _attribution_events(claims, unit, win_start, win_end, emit) -> None:
    """Sweep one unit's claims and ``emit`` each attribution segment.

    Events live in compact integer arrays rather than one Python tuple per
    event; active state is a small integer set keyed by claim id.  Half-open
    ordering (ends before starts at equal instants) prevents back-to-back
    claims from manufacturing a zero-length conflict.
    """
    from array import array
    ev_t = array("q")
    ev_kind = array("b")
    ev_cid = array("i")
    by_cid: dict[int, Claim] = {}
    for c in claims:
        if c.unit_id != unit:
            continue
        by_cid[c.claim_id] = c
        ev_t.append(c.start); ev_kind.append(1); ev_cid.append(c.claim_id)
        ev_t.append(c.end);   ev_kind.append(0); ev_cid.append(c.claim_id)
    order = sorted(range(len(ev_t)),
                   key=lambda i: (ev_t[i], ev_kind[i]))
    active: set = set()
    cur = win_start
    cur_state, cur_visit, cur_conf, cur_cids = _attr_snapshot(active, by_cid)
    for idx in order:
        t, kind, cid = int(ev_t[idx]), ev_kind[idx], ev_cid[idx]
        if t > cur:
            emit((cur, t, cur_state, cur_visit,
                  list(cur_conf), list(cur_cids)))
            cur = t
        if kind == 1:
            active.add(cid)
        else:
            active.discard(cid)
        cur_state, cur_visit, cur_conf, cur_cids = _attr_snapshot(
            active, by_cid)
    if cur < win_end:
        emit((cur, win_end, cur_state, cur_visit,
              list(cur_conf), list(cur_cids)))


def _attr_snapshot(active: set, by_cid: dict):
    if not active:
        return AT_NONE, None, (), ()
    cids = sorted(active)
    # Reuse the already-sorted claim ids to derive distinct visits in order
    # without building an intermediate set, which this hot path did per event.
    visits: list = []
    seen: set = set()
    for cid in cids:
        vid = by_cid[cid].visit_id
        if vid not in seen:
            seen.add(vid)
            visits.append(vid)
    if len(visits) == 1:
        return AT_UNIQUE, visits[0], (), cids
    return AT_CONFLICT, None, visits, cids


def _release_free_memory() -> None:
    """Return freed allocator arenas to the OS where the libc supports it.

    CPython's small-object allocator keeps freed arenas mapped, so a stream
    of large per-unit temporaries can raise the RSS high-water mark even
    though the objects are unreachable.  A glibc ``malloc_trim(0)`` releases
    those pages; on other platforms the symbol is absent and this is a no-op.
    """
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass


def _classify(measured: str, attribution: str) -> str:
    if attribution == AT_CONFLICT:
        return CLS_ATTR_CONFLICT
    if measured == MS_UNKNOWN:
        return CLS_PENDING
    if measured == MS_OCCUPIED:
        return CLS_UNATTRIBUTED if attribution == AT_NONE \
            else CLS_MATCH_OCCUPIED
    # measured idle
    return CLS_MISMATCH if attribution == AT_UNIQUE else CLS_MATCH_IDLE


# ================================================================ whole run
def run_reconciliation(wh: Warehouse, inputs: StandInputs, *, airport: str,
                       window_start: str, window_end: str,
                       receipt_batch: int, cutoff: str,
                       segment_sink=None) -> dict:
    """Run one audit.

    ``segment_sink`` (optional) receives every final segment dict as soon as
    its unit has been merged, so a scale run never holds the whole time ledger
    in memory.  When no sink is given the segments are also returned in the
    result under ``segments``.
    """
    wh.require_published(receipt_batch)
    try:
        ws = parse_offset_datetime(window_start)
        we = parse_offset_datetime(window_end)
        co = parse_offset_datetime(cutoff)
    except Exception as exc:  # noqa: BLE001
        raise ReconError("bad_time_parameter", str(exc)) from exc
    win_start, win_end, cutoff_us = to_us(ws), to_us(we), to_us(co)
    if not win_start < win_end:
        raise ReconError("bad_window",
                         "window start must be strictly before end")
    if cutoff_us < win_end:
        raise ReconError("cutoff_before_window_end",
                         "the fact cutoff must not be earlier than the window "
                         "end (half-open window)")

    from .stand_config import check_map_covers_window
    check_map_covers_window(inputs, win_start, win_end)
    versions = sorted(inputs.map_versions,
                      key=lambda v: (to_us(v.start), to_us(v.end)))
    units = sorted(u.unit_id for u in inputs.units)
    issues: list[Issue] = []

    # ---- 1. resolve visits against authoritative facts (single SQL pass)
    roots = set()
    for v in inputs.visits:
        roots.add(v.arrival_root)
        roots.add(v.departure_root)
    winners = scan_actuals(wh, receipt_batch, co.utc, roots)
    graph = wh.load_graph(receipt_batch)

    visits_out: dict[str, VisitOutcome] = {}
    for vrec in inputs.visits:
        vo = VisitOutcome(
            visit_id=vrec.visit_id, status="determinable",
            arrival_root=vrec.arrival_root, departure_root=vrec.departure_root,
            source_id=vrec.source_id)

        def fail(reason, detail):
            vo.status = "unavailable"
            vo.reason, vo.reason_detail = reason, detail
            visits_out[vrec.visit_id] = vo
            issues.append(Issue(I_UNAVAILABLE_VISIT,
                                f"visit {vrec.visit_id!r}: {detail}",
                                vrec.source_id, visit_id=vrec.visit_id))

        ar_il = graph.initials.get(vrec.arrival_root)
        dr_il = graph.initials.get(vrec.departure_root)
        if ar_il is None or dr_il is None:
            missing = [r for r, il in ((vrec.arrival_root, ar_il),
                                       (vrec.departure_root, dr_il))
                       if il is None]
            fail(R_UNKNOWN_ROOT,
                 f"unknown operating root(s) {missing} in batch {receipt_batch}")
            continue
        if not ar_il.key or ar_il.key.arrival != airport:
            fail(R_AIRPORT_MISMATCH,
                 f"inbound root {vrec.arrival_root!r} arrives at "
                 f"{ar_il.key.arrival if ar_il.key else None!r}, not "
                 f"{airport!r}")
            continue
        if not dr_il.key or dr_il.key.departure != airport:
            fail(R_AIRPORT_MISMATCH,
                 f"outbound root {vrec.departure_root!r} departs from "
                 f"{dr_il.key.departure if dr_il.key else None!r}, not "
                 f"{airport!r}")
            continue

        arr_fr = adjudicate(winners[vrec.arrival_root].get("act_arr"))
        dep_fr = adjudicate(winners[vrec.departure_root].get("act_dep"))
        vo.arr_evidence = evidence_of(arr_fr)
        vo.dep_evidence = evidence_of(dep_fr)
        if arr_fr.state != "value" or not arr_fr.value:
            fail(R_ACTUAL_UNCERTAIN,
                 f"actual arrival of {vrec.arrival_root!r} is not definite "
                 f"(field state {arr_fr.state}); scheduled times are never "
                 f"substitutes")
            continue
        if dep_fr.state != "value" or not dep_fr.value:
            fail(R_ACTUAL_UNCERTAIN,
                 f"actual departure of {vrec.departure_root!r} is not definite "
                 f"(field state {dep_fr.state}); scheduled times are never "
                 f"substitutes")
            continue
        arr_t = parse_offset_datetime(arr_fr.value)
        dep_t = parse_offset_datetime(dep_fr.value)
        if to_us(arr_t) > cutoff_us or to_us(dep_t) > cutoff_us:
            late = (vrec.arrival_root if to_us(arr_t) > cutoff_us
                    else vrec.departure_root)
            fail(R_AFTER_CUTOFF,
                 f"actual time of {late!r} is later than the fact cutoff "
                 f"{co.iso_utc()}")
            continue
        if not to_us(dep_t) > to_us(arr_t):
            fail(R_DEP_NOT_AFTER_ARR,
                 f"departure {dep_t.iso_utc()} is not strictly after arrival "
                 f"{arr_t.iso_utc()}")
            continue
        vo.ground_start, vo.ground_end = to_us(arr_t), to_us(dep_t)
        visits_out[vrec.visit_id] = vo

    # ---- 2. ledger rows -> physical-unit claims (map-aware clipping)
    ledger_by_visit: dict[str, list[LedgerRecord]] = {}
    for rec in inputs.ledger:
        ledger_by_visit.setdefault(rec.visit_id, []).append(rec)

    all_claims: list[Claim] = []
    # Compact positional rows: (record_id, visit_id, stand, start_us,
    # end_us, source_id, usable, issue_kind).  Tuples avoid a 200k-row dict
    # allocation at scale.
    stored_ledger_rows: list[tuple] = []
    claim_seq = 0
    for rec in inputs.ledger:
        def _store(usable: int, issue_kind):
            stored_ledger_rows.append((
                rec.record_id, rec.visit_id, rec.stand, rec.start_us,
                rec.end_us, rec.source_id, usable, issue_kind))
        vo = visits_out.get(rec.visit_id)
        if vo is None:
            _store(0, I_UNKNOWN_VISIT_LEDGER)
            issues.append(Issue(
                I_UNKNOWN_VISIT_LEDGER,
                f"ledger row {rec.record_id!r} names unknown visit "
                f"{rec.visit_id!r}", rec.source_id,
                visit_id=rec.visit_id, ledger_record_id=rec.record_id))
            continue
        if vo.status != "determinable":
            _store(0, I_UNAVAILABLE_VISIT_LEDGER)
            issues.append(Issue(
                I_UNAVAILABLE_VISIT_LEDGER,
                f"ledger row {rec.record_id!r} belongs to unavailable visit "
                f"{rec.visit_id!r} ({vo.reason})", rec.source_id,
                visit_id=rec.visit_id, ledger_record_id=rec.record_id))
            continue
        gs, ge = vo.ground_start, vo.ground_end
        if rec.end_us <= gs or rec.start_us >= ge:
            _store(0, I_DISJOINT_LEDGER)
            issues.append(Issue(
                I_DISJOINT_LEDGER,
                f"ledger row {rec.record_id!r} (microsecond interval "
                f"[{rec.start_us}, {rec.end_us})) does not intersect visit "
                f"{rec.visit_id!r} ground interval; kept for inspection",
                rec.source_id, visit_id=rec.visit_id,
                ledger_record_id=rec.record_id))
            continue

        lo0, hi0 = max(rec.start_us, gs, win_start), min(rec.end_us, ge,
                                                         win_end)
        if lo0 >= hi0:
            # ground-intersecting in principle, but wholly outside the audit
            # window: still a traceable non-participating input.
            _store(0, I_DISJOINT_LEDGER)
            issues.append(Issue(
                I_DISJOINT_LEDGER,
                f"ledger row {rec.record_id!r} intersects visit "
                f"{rec.visit_id!r} ground interval only outside the audit "
                f"window; kept for inspection",
                rec.source_id, visit_id=rec.visit_id,
                ledger_record_id=rec.record_id))
            continue
        row_usable = False
        for lo, hi, ver in split_on_map(lo0, hi0, versions):
            if lo >= hi:
                continue
            if rec.stand not in ver.mapping:
                issues.append(Issue(
                    I_MAPPING_GAP,
                    f"ledger row {rec.record_id!r}: logical stand "
                    f"{rec.stand!r} is not registered in map version "
                    f"{ver.version_id!r} over microseconds [{lo}, {hi}); this "
                    f"part is an attribution gap, no physical unit is invented "
                    f"and the mappable parts of the same claim are retained",
                    rec.source_id, visit_id=rec.visit_id,
                    ledger_record_id=rec.record_id))
                continue
            for unit in ver.mapping[rec.stand]:
                claim_seq += 1
                all_claims.append(Claim(
                    claim_seq, rec.record_id, rec.visit_id, rec.stand, unit,
                    lo, hi, ver.version_id, rec.source_id))
                row_usable = True
        _store(int(row_usable),
               None if row_usable else I_MAPPING_GAP)

    # ---- 3. per-visit simultaneous multi-stand conflicts.
    # A visit declaring >=2 distinct logical stands at one instant puts ALL
    # units reached by its mappable claims at that instant in conflict — even
    # if the stands map to identical units or one stand is unmapped.
    conflict_intervals_by_visit: dict[str, list] = {}
    for vid, recs in ledger_by_visit.items():
        vo = visits_out.get(vid)
        if vo is None or vo.status != "determinable":
            continue
        stand_ivs: dict[str, list[tuple[int, int]]] = {}
        for rec in recs:
            if rec.end_us <= vo.ground_start or rec.start_us >= vo.ground_end:
                continue
            lo = max(rec.start_us, vo.ground_start, win_start)
            hi = min(rec.end_us, vo.ground_end, win_end)
            if lo < hi:
                stand_ivs.setdefault(rec.stand, []).append((lo, hi))
        if len(stand_ivs) < 2:
            continue
        events: list = []
        for stand, ivs in stand_ivs.items():
            for lo, hi in ivs:
                events.append((lo, 1, stand))
                events.append((hi, 0, stand))
        events.sort(key=lambda e: (e[0], e[1]))
        active_stands: set[str] = set()
        marks: list[tuple[int, int]] = []
        mark_start = None
        for t, kind, stand in events:
            if kind == 1:
                active_stands.add(stand)
            else:
                active_stands.discard(stand)
            if len(active_stands) >= 2 and mark_start is None:
                mark_start = t
            elif len(active_stands) < 2 and mark_start is not None:
                if t > mark_start:
                    marks.append((mark_start, t))
                mark_start = None
        if mark_start is not None and win_end > mark_start:
            marks.append((mark_start, win_end))
        if marks:
            conflict_intervals_by_visit[vid] = marks

    # ---- 4. per-unit sweeps, merged and streamed one unit at a time.  Only
    # claims of the current unit are touched, so a sensor change never copies
    # the whole hot-unit ledger.  Observation blocks are already grouped by
    # unit (compact arrays), so no O(records * units) grouping happens here.
    claims_by_unit: dict[str, list[Claim]] = {u: [] for u in units}
    claims_by_visit: dict[str, list[Claim]] = {}
    for cl in all_claims:
        claims_by_unit[cl.unit_id].append(cl)
        claims_by_visit.setdefault(cl.visit_id, []).append(cl)

    class_totals = {c: 0 for c in ALL_CLASSES}
    per_unit_totals: dict[str, dict] = {}
    lower = upper = 0
    segment_count = 0
    final_segments: list[dict] = []

    # Multi-stand overlay, restricted to units the conflicting visit actually
    # reaches with a mappable claim.  Per unit we build a sorted sweep table
    # of (instant, +1/-1) events, so deciding whether an overlay is active at
    # a segment boundary is a bisect lookup, never a segment-by-visit scan.
    import bisect
    overlay_events: dict[str, list] = {u: [] for u in units}
    for vid, marks in conflict_intervals_by_visit.items():
        per_unit_ivs: dict[str, list[tuple[int, int]]] = {}
        for cl in claims_by_visit.get(vid, ()):
            for mlo, mhi in marks:
                lo, hi = max(cl.start, mlo), min(cl.end, mhi)
                if lo < hi:
                    per_unit_ivs.setdefault(cl.unit_id, []).append((lo, hi))
        for uid, ivs in per_unit_ivs.items():
            ev = overlay_events[uid]
            for lo, hi in ivs:
                ev.append((lo, 1, vid))
                ev.append((hi, -1, vid))
    overlay_sorted: dict[str, tuple] = {}
    for u in units:
        ev = sorted(overlay_events[u], key=lambda e: (e[0], e[1]))
        overlay_sorted[u] = ([t for t, _d, _v in ev],
                             [d for _t, d, _v in ev],
                             [v for _t, _d, v in ev])

    for unit in units:
        a_segs = build_attribution_for_unit(claims_by_unit[unit], unit,
                                            win_start, win_end)
        s_segs = build_sensor_timeline(inputs, win_start, win_end, issues,
                                       unit)
        # Both sweeps emit gap-free, ordered segments, so their boundaries are
        # already sorted sequences.  Merge them by index (no million-entry
        # Python set) into one raw-int array of distinct breakpoints.
        from array import array
        a_bounds = sorted(b for lo, hi, *_ in a_segs for b in (lo, hi)
                          if win_start <= b <= win_end)
        s_bounds = sorted(b for lo, hi, *_ in s_segs for b in (lo, hi)
                          if win_start <= b <= win_end)
        merged = array("q", [win_start])
        ia = ib = 0
        while ia < len(a_bounds) or ib < len(s_bounds):
            na = a_bounds[ia] if ia < len(a_bounds) else None
            nb = s_bounds[ib] if ib < len(s_bounds) else None
            if nb is None or (na is not None and na <= nb):
                cand, ia = na, ia + 1
            else:
                cand, ib = nb, ib + 1
            if cand != merged[-1] and cand <= win_end:
                merged.append(cand)
        if merged[-1] != win_end:
            merged.append(win_end)
        del a_bounds, s_bounds
        ai = si = 0
        # Rows are emitted as soon as they are built; only running totals and
        # the conservation sum survive the loop, so a unit with tens of
        # thousands of segments never holds them all as live dicts.
        unit_class = {c: 0 for c in ALL_CLASSES}
        unit_lower = unit_upper = unit_total = 0
        unit_segment_count = 0
        o_times, o_deltas, o_vids = overlay_sorted[unit]
        ev_cursor = 0
        overlay_active: set = set()
        for k in range(len(merged) - 1):
            lo, hi = int(merged[k]), int(merged[k + 1])
            while ev_cursor < len(o_times) and o_times[ev_cursor] < hi:
                if o_deltas[ev_cursor] > 0:
                    overlay_active.add(o_vids[ev_cursor])
                else:
                    overlay_active.discard(o_vids[ev_cursor])
                ev_cursor += 1
            while a_segs[ai][1] <= lo:
                ai += 1
            while s_segs[si][1] <= lo:
                si += 1
            a = a_segs[ai]
            s = s_segs[si]
            m_state, m_reason, m_evidence = s[2], s[3], s[4]
            a_state, a_visit, a_conflicts, a_claim_ids = (
                a[2], a[3], a[4], a[5])

            if overlay_active:
                visit_set = set(a_conflicts)
                if a_state == AT_UNIQUE and a_visit:
                    visit_set.add(a_visit)
                visit_set |= overlay_active
                a_state, a_visit = AT_CONFLICT, None
                a_conflicts = tuple(sorted(visit_set))

            recon_cls = _classify(m_state, a_state)
            duration = hi - lo
            # Build delimited text lazily and only for non-empty lists: half
            # the segments have no conflicts/claims and need no join at all.
            conflict_text = ",".join(a_conflicts) if a_conflicts else ""
            evidence_text = ",".join(str(sid) for sid in m_evidence) \
                if m_evidence else ""
            claims_text = ",".join(map(str, a_claim_ids)) \
                if a_claim_ids else ""
            unit_class[recon_cls] += duration
            unit_total += duration
            if m_state == MS_OCCUPIED:
                unit_lower += duration
            if m_state in (MS_OCCUPIED, MS_UNKNOWN):
                unit_upper += duration
            class_totals[recon_cls] += duration
            if m_state == MS_OCCUPIED:
                lower += duration
            if m_state in (MS_OCCUPIED, MS_UNKNOWN):
                upper += duration
            if segment_sink is not None:
                # compact positional tuple: no per-segment dict on the
                # million-row streaming path
                segment_sink((
                    unit, lo, hi, duration, m_state, m_reason, a_state,
                    a_visit, conflict_text, recon_cls, evidence_text,
                    claims_text))
            else:
                final_segments.append({
                    "unit_id": unit, "start_us": lo, "end_us": hi,
                    "duration_us": duration,
                    "measured_state": m_state, "unknown_reason": m_reason,
                    "attribution": a_state, "visit_id": a_visit,
                    "conflict_visits": list(a_conflicts),
                    "recon_class": recon_cls,
                    "measured_evidence": m_evidence,
                    "measured_evidence_text": evidence_text,
                    "claim_ids": list(a_claim_ids),
                    "claim_ids_text": claims_text,
                })
            segment_count += 1
            unit_segment_count += 1
        # conservation per unit before the unit's intermediates are dropped
        if unit_total != win_end - win_start:
            raise ReconError("conservation_violated",
                             f"unit {unit}: segments do not partition the "
                             f"window without gaps or overlaps")
        per_unit_totals[unit] = {
            "window_us": win_end - win_start,
            "classes": unit_class,
            "measured_occupied_us_lower": unit_lower,
            "measured_occupied_us_upper": unit_upper}
        # Drop the unit's sweep intermediates and reclaim their arenas before
        # the next unit, so the high-water mark does not accumulate across
        # units that each emit tens of thousands of segments.  On glibc
        # systems malloc_trim returns the freed pymalloc arenas to the OS;
        # elsewhere the call is a harmless no-op.
        del a_segs, s_segs, merged
        gc.collect()
        _release_free_memory()

    summary = _summarize(class_totals, per_unit_totals, units, win_start,
                         win_end, visits_out, inputs, issues, all_claims,
                         lower, upper, segment_count)
    return {
        "airport": airport,
        "window": {"start": ws.iso_utc(), "end": we.iso_utc(),
                   "start_text": window_start, "end_text": window_end,
                   "duration_us": win_end - win_start},
        "receipt_batch": receipt_batch,
        "cutoff": {"text": cutoff, "utc": co.iso_utc()},
        "units": units,
        "visits": list(visits_out.values()),
        "issues": issues,
        "claims": all_claims,
        "stored_ledger_rows": stored_ledger_rows,
        "segments": final_segments,
        "summary": summary,
        "online_rows": inputs.online,
        "obs_blocks": inputs.obs_blocks,
        "sources": inputs.sources,
        "map_versions": versions,
        "win_start_us": win_start,
        "win_end_us": win_end,
    }


def _summarize(class_totals, per_unit_totals, units, win_start, win_end,
               visits_out, inputs, issues, claims, lower, upper,
               segment_count) -> dict:
    window_us = win_end - win_start
    per_unit: dict[str, dict] = {}
    for u in units:
        t = per_unit_totals[u]
        per_unit[u] = dict(t["classes"])
        per_unit[u]["window_us"] = window_us
        per_unit[u]["measured_occupied_us_lower"] = \
            t["measured_occupied_us_lower"]
        per_unit[u]["measured_occupied_us_upper"] = \
            t["measured_occupied_us_upper"]
        if sum(t["classes"].values()) != window_us:  # pragma: no cover
            raise ReconError("conservation_violated",
                             f"unit {u}: class durations do not sum to the "
                             f"window length")
    return {
        "unit_count": len(units),
        "window_us": window_us,
        "unit_time_total_us": window_us * len(units),
        "class_totals_us": class_totals,
        "per_unit_us": per_unit,
        "measured_occupied_lower_bound_us": lower,
        "measured_occupied_upper_bound_us": upper,
        "visits_total": len(inputs.visits),
        "visits_determinable": sum(
            1 for v in visits_out.values() if v.status == "determinable"),
        "visits_unavailable": sum(
            1 for v in visits_out.values() if v.status == "unavailable"),
        "ledger_records": len(inputs.ledger),
        "physical_claim_intervals": len(claims),
        "issue_count": len(issues),
        "observation_records": sum(len(b) for b in
                                   inputs.obs_blocks.values()),
        "online_records": len(inputs.online),
        "segment_count": segment_count,
    }
