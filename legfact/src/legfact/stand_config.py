"""Input formats of the offline stand-occupancy reconciliation mart.

Four machine-readable, *line-processable* NDJSON inputs feed one audit run.
Every physical line is one JSON object carrying its own ``record_id``; the
loader also retains the source file name and the 1-based line number so every
downstream row can be traced back to an exact input location.

Memory design (the scale rehearsal holds a million observations):

* every source location is interned once into a :class:`SourceTable` and the
  records carry a small integer source id instead of per-row objects;
* observations are kept per physical unit in compact ``array`` blocks of
  64-bit integers, not one Python object per observation;
* interval records use exact integer UTC microseconds and ``__slots__``.

Inputs (all time intervals half-open ``[start, end)``, every timestamp an
ISO-8601 string with an explicit UTC offset; instants are compared by their
absolute UTC moment while the original text stays locatable through the
source id).
"""
from __future__ import annotations

import json
import os
from array import array
from dataclasses import dataclass, field

from .timeutil import OffsetDateTime, TimeParseError, parse_offset_datetime

# Input stream kinds (the ``type`` field every NDJSON line must carry).
T_VISIT = "stand_visit"
T_LEDGER = "stand_usage"
T_UNIT = "physical_unit"
T_MAP = "stand_map_version"
T_ONLINE = "online_interval"
T_OBS = "observation"

# Observation states as written in the input, plus compact integer codes.
OBS_IDLE = "idle"
OBS_OCCUPIED = "occupied"
STATE_CODE = {OBS_IDLE: 0, OBS_OCCUPIED: 1}
CODE_STATE = {0: OBS_IDLE, 1: OBS_OCCUPIED}


class StandInputError(ValueError):
    """A structural problem that must fail the whole reconciliation run."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


class SourceTable:
    """Interned input locations: source id -> (file, line, record id).

    Two storage modes share the same integer source-id contract:

    * **memory mode** (default): every location is retained in Python
      containers.  Fine for small fixtures and tests.
    * **scratch mode** (a ``sqlite3`` connection given at construction): the
      full locations stream into a scratch table while the loader runs, and
      the in-memory footprint stays three compact integer arrays (line, file
      code, record-id code).  This is what keeps a million-observation input
      under the RSS budget; the scratch rows are copied into the published
      sidecar with one ``INSERT ... SELECT`` at the end.

    Duplicate primary keys are rejected by the loader per input stream, so
    this table only interns locations and never has to deduplicate record ids
    itself (the file name disambiguates same-named rows across streams).
    """

    def __init__(self, scratch_conn=None, scratch_path: str | None = None):
        self._file_ids: dict[str, int] = {}
        self.file_names: list[str] = []
        self.lines = array("i")
        self._file_codes = array("i")
        self._scratch = scratch_conn
        self.scratch_path = scratch_path
        self._pending: list[tuple] = []
        self._flush_at = 20000
        if scratch_conn is not None:
            scratch_conn.execute(
                "CREATE TABLE IF NOT EXISTS scratch_sources ("
                "source_id INTEGER PRIMARY KEY, file TEXT NOT NULL, "
                "line INTEGER NOT NULL, record_id TEXT NOT NULL)")
            self._rid_codes = None
            self._rid_ids = None
            self.rid_texts = None
        else:
            self._rid_ids: dict[str, int] = {}
            self.rid_texts: list[str] = []
            self._rid_codes = array("i")

    def intern(self, file: str, line: int, record_id: str) -> int:
        fid = self._file_ids.get(file)
        if fid is None:
            fid = len(self.file_names)
            self._file_ids[file] = fid
            self.file_names.append(file)
        sid = len(self.lines)
        self.lines.append(line)
        self._file_codes.append(fid)
        if self._scratch is not None:
            self._pending.append((sid, file, line, record_id))
            if len(self._pending) >= self._flush_at:
                self.flush_scratch()
        else:
            rid_code = self._rid_ids.get(record_id)
            if rid_code is None:
                rid_code = len(self.rid_texts)
                self._rid_ids[record_id] = rid_code
                self.rid_texts.append(record_id)
            self._rid_codes.append(rid_code)
        return sid

    def flush_scratch(self) -> None:
        """Force buffered scratch rows into SQLite (called at load end too)."""
        if self._scratch is None or not self._pending:
            return
        self._scratch.executemany(
            "INSERT INTO scratch_sources(source_id,file,line,record_id) "
            "VALUES(?,?,?,?)", self._pending)
        self._scratch.commit()
        self._pending.clear()

    def reopen_scratch_readonly(self):
        """Close the writer handle and reopen read-only for lookups.

        Frees the write lock so the publishing sidecar can ATTACH this file,
        while the engine can still resolve source locations on demand.
        """
        if self._scratch is None or self.scratch_path is None:
            return
        self.flush_scratch()
        self._scratch.close()
        import sqlite3
        self._scratch = sqlite3.connect(
            f"file:{self.scratch_path}?mode=ro", uri=True)

    @property
    def scratch_conn(self):
        return self._scratch

    def resolve(self, source_id: int) -> dict:
        if self._scratch is not None:
            r = self._scratch.execute(
                "SELECT file, line, record_id FROM scratch_sources "
                "WHERE source_id=?", (source_id,)).fetchone()
            return {"file": r[0], "line": int(r[1]), "record_id": r[2]}
        return {"file": self.file_names[self._file_codes[source_id]],
                "line": int(self.lines[source_id]),
                "record_id": self.rid_texts[self._rid_codes[source_id]]}

    def rows(self):
        if self._scratch is not None:
            self.flush_scratch()
            cur = self._scratch.execute(
                "SELECT source_id, file, line, record_id FROM scratch_sources "
                "ORDER BY source_id")
            for sid, file, line, record_id in cur:
                yield sid, {"file": file, "line": int(line),
                            "record_id": record_id}
            return
        for sid in range(len(self.lines)):
            yield sid, self.resolve(sid)


# --------------------------------------------------------------------- visits
@dataclass(slots=True)
class VisitRecord:
    visit_id: str
    arrival_root: str
    departure_root: str
    source_id: int


# --------------------------------------------------------------------- ledger
@dataclass(slots=True)
class LedgerRecord:
    record_id: str
    visit_id: str
    stand: str
    start_us: int
    end_us: int
    source_id: int


# ------------------------------------------------------------------------ map
@dataclass(slots=True)
class PhysicalUnit:
    unit_id: str
    source_id: int


@dataclass(slots=True)
class MapVersion:
    version_id: str
    start: OffsetDateTime
    end: OffsetDateTime
    mapping: dict[str, tuple[str, ...]]
    source_id: int


# --------------------------------------------------------------------- sensor
@dataclass(slots=True)
class OnlineInterval:
    record_id: str
    unit_id: str
    start_us: int
    end_us: int
    source_id: int


class ObsBlock:
    """Compact observations of one unit.

    ``at`` holds absolute UTC microseconds (signed 64-bit); ``packed`` packs
    the source id in the high bits and the 1-bit state code in the low bit.
    Both are :class:`array.array` blocks, so a million observations cost
    ~16 MiB rather than ~100 MiB of Python objects.
    """

    __slots__ = ("unit_id", "at", "packed")

    def __init__(self, unit_id: str):
        self.unit_id = unit_id
        self.at = array("q")
        self.packed = array("Q")

    def add(self, at_us: int, state_code: int, source_id: int) -> None:
        self.at.append(at_us)
        self.packed.append((source_id << 1) | state_code)

    def sort(self) -> None:
        order = sorted(range(len(self.at)), key=lambda i: self.at[i])
        self.at = array("q", (self.at[i] for i in order))
        self.packed = array("Q", (self.packed[i] for i in order))

    def __len__(self) -> int:
        return len(self.at)


@dataclass
class StandInputs:
    """Everything parsed from the four input files."""

    sources: SourceTable = field(default_factory=SourceTable)
    visits: list[VisitRecord] = field(default_factory=list)
    ledger: list[LedgerRecord] = field(default_factory=list)
    units: list[PhysicalUnit] = field(default_factory=list)
    map_versions: list[MapVersion] = field(default_factory=list)
    online: list[OnlineInterval] = field(default_factory=list)
    obs_blocks: dict[str, ObsBlock] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)

    @property
    def observations(self):
        """Yield (unit_id, at_us, state_code, source_id) in storage order."""
        for unit, blk in self.obs_blocks.items():
            for i, at_us in enumerate(blk.at):
                packed = int(blk.packed[i])
                yield unit, at_us, packed & 1, packed >> 1


# ----------------------------------------------------------------- utilities
def _to_us(dt) -> int:
    """Absolute UTC instant as exact integer microseconds since the epoch."""
    from datetime import timezone
    epoch = dt.astimezone(timezone.utc)
    return int(epoch.timestamp()) * 1_000_000 + epoch.microsecond


def to_us(odt: OffsetDateTime) -> int:
    return _to_us(odt.utc)


def _require(d: dict, key: str, src) -> object:
    if key not in d:
        raise StandInputError(
            "missing_field",
            f"record {src[2]!r} ({src[0]}:{src[1]}) missing {key!r}")
    return d[key]


def _nonempty_str(v, key: str, src) -> str:
    if not isinstance(v, str) or not v.strip():
        raise StandInputError(
            "bad_field",
            f"record {src[2]!r} ({src[0]}:{src[1]}): {key} must be a "
            f"non-empty string, got {v!r}")
    return v


def _parse_ts(v, key: str, src) -> OffsetDateTime:
    if not isinstance(v, str):
        raise StandInputError(
            "bad_timestamp",
            f"record {src[2]!r} ({src[0]}:{src[1]}): {key} must be an "
            f"ISO-8601 string with explicit offset, got {v!r}")
    try:
        return parse_offset_datetime(v)
    except TimeParseError as exc:
        raise StandInputError(
            "bad_timestamp",
            f"record {src[2]!r} ({src[0]}:{src[1]}): {key} {exc}") from exc


def _half_open(rec_start, rec_end, src) -> None:
    if not rec_start.utc < rec_end.utc:
        raise StandInputError(
            "bad_interval",
            f"record {src[2]!r} ({src[0]}:{src[1]}): interval start "
            f"{rec_start.original!r} must be strictly before end "
            f"{rec_end.original!r} (half-open [start,end))")


def _iter_ndjson(path: str, role: str, sources: SourceTable):
    """Yield (parsed_object, (file, line, record_id), source_id) per line.

    Streams the file instead of materializing the whole byte/string content,
    which is what keeps a million-line sensor input from ballooning the
    process high-water mark.
    """
    fname = os.path.basename(path)
    count = 0
    try:
        fh = open(path, "rb")
    except OSError as exc:
        raise StandInputError(
            "unreadable_input", f"cannot read {role} file {path}: {exc}") \
            from exc
    with fh:
        for lineno, raw_line in enumerate(fh, start=1):
            if not raw_line.strip():
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StandInputError(
                    "bad_encoding",
                    f"{role} file {fname} is not UTF-8: {exc}") from exc
            try:
                d = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StandInputError(
                    "malformed_json",
                    f"{fname}:{lineno}: not a JSON object: {exc}") from exc
            if not isinstance(d, dict):
                raise StandInputError(
                    "malformed_json",
                    f"{fname}:{lineno}: each line must be a JSON object, got "
                    f"{type(d).__name__}")
            rid = d.get("record_id")
            if not isinstance(rid, str) or not rid.strip():
                raise StandInputError(
                    "missing_record_id",
                    f"{fname}:{lineno}: every record needs a non-empty "
                    f"string record_id")
            sid = sources.intern(fname, lineno, rid)
            count += 1
            yield d, (fname, lineno, rid), sid
    if not count:
        raise StandInputError("empty_input",
                              f"{role} file {fname} contains no records")


def _read_ndjson(path: str, role: str, sources: SourceTable):
    return list(_iter_ndjson(path, role, sources))


def load_inputs(*, roster_path: str, ledger_path: str, map_path: str,
                sensor_path: str, scratch_conn=None,
                scratch_path: str | None = None) -> StandInputs:
    """Parse and structurally validate the four NDJSON input files.

    When ``scratch_conn`` is given, source locations spill into that SQLite
    scratch table instead of being retained in memory (scale runs pass a
    temporary connection); otherwise everything stays in memory.
    """
    inputs = StandInputs(sources=SourceTable(scratch_conn, scratch_path))
    src = inputs.sources
    inputs.files = {"visit_roster": os.path.basename(roster_path),
                    "usage_ledger": os.path.basename(ledger_path),
                    "stand_map": os.path.basename(map_path),
                    "sensor": os.path.basename(sensor_path)}

    # ---- 1. visit roster
    seen_visits: set[str] = set()
    arr_owner: dict[str, str] = {}
    dep_owner: dict[str, str] = {}
    for d, loc, sid in _read_ndjson(roster_path, "visit_roster", src):
        if d.get("type") != T_VISIT:
            raise StandInputError(
                "bad_record_type",
                f"{loc[0]}:{loc[1]}: expected type {T_VISIT!r}, got "
                f"{d.get('type')!r}")
        vid = _nonempty_str(d.get("visit_id"), "visit_id", loc)
        if vid in seen_visits:
            raise StandInputError(
                "duplicate_visit",
                f"{loc[0]}:{loc[1]}: visit_id {vid!r} appears more than once")
        seen_visits.add(vid)
        ar = _nonempty_str(_require(d, "arrival_root", loc),
                           "arrival_root", loc)
        dr = _nonempty_str(_require(d, "departure_root", loc),
                           "departure_root", loc)
        if ar == dr:
            raise StandInputError(
                "same_root_both_roles",
                f"{loc[0]}:{loc[1]}: visit {vid!r} names the same root "
                f"{ar!r} as both arrival and departure root")
        if ar in arr_owner:
            raise StandInputError(
                "root_role_reuse",
                f"{loc[0]}:{loc[1]}: arrival root {ar!r} already assigned to "
                f"visit {arr_owner[ar]!r}; a root cannot fill one role twice")
        if dr in dep_owner:
            raise StandInputError(
                "root_role_reuse",
                f"{loc[0]}:{loc[1]}: departure root {dr!r} already assigned "
                f"to visit {dep_owner[dr]!r}; a root cannot fill one role "
                f"twice")
        arr_owner[ar] = vid
        dep_owner[dr] = vid
        inputs.visits.append(VisitRecord(vid, ar, dr, sid))

    # ---- 2. usage ledger
    seen_ledger: set[str] = set()
    for d, loc, sid in _iter_ndjson(ledger_path, "usage_ledger", src):
        if d.get("type") != T_LEDGER:
            raise StandInputError(
                "bad_record_type",
                f"{loc[0]}:{loc[1]}: expected type {T_LEDGER!r}, got "
                f"{d.get('type')!r}")
        rid = loc[2]
        if rid in seen_ledger:
            raise StandInputError(
                "duplicate_ledger_record",
                f"{loc[0]}:{loc[1]}: record_id {rid!r} appears more than once")
        seen_ledger.add(rid)
        vid = _nonempty_str(_require(d, "visit_id", loc), "visit_id", loc)
        stand = _nonempty_str(_require(d, "stand", loc), "stand", loc)
        st = _parse_ts(_require(d, "start", loc), "start", loc)
        en = _parse_ts(_require(d, "end", loc), "end", loc)
        _half_open(st, en, loc)
        inputs.ledger.append(
            LedgerRecord(rid, vid, stand, _to_us(st.utc), _to_us(en.utc),
                         sid))

    # ---- 3. stand map: physical units + versioned complete mappings
    seen_units: set[str] = set()
    seen_versions: set[str] = set()
    for d, loc, sid in _read_ndjson(map_path, "stand_map", src):
        kind = d.get("type")
        if kind == T_UNIT:
            uid = _nonempty_str(d.get("unit_id"), "unit_id", loc)
            if uid in seen_units:
                raise StandInputError(
                    "duplicate_unit",
                    f"{loc[0]}:{loc[1]}: physical unit {uid!r} declared twice")
            seen_units.add(uid)
            inputs.units.append(PhysicalUnit(uid, sid))
        elif kind == T_MAP:
            vid = _nonempty_str(d.get("version_id"), "version_id", loc)
            if vid in seen_versions:
                raise StandInputError(
                    "duplicate_map_version",
                    f"{loc[0]}:{loc[1]}: map version {vid!r} declared twice")
            seen_versions.add(vid)
            st = _parse_ts(_require(d, "start", loc), "start", loc)
            en = _parse_ts(_require(d, "end", loc), "end", loc)
            _half_open(st, en, loc)
            mp_raw = _require(d, "mapping", loc)
            if not isinstance(mp_raw, dict) or not mp_raw:
                raise StandInputError(
                    "bad_mapping",
                    f"{loc[0]}:{loc[1]}: mapping must be a non-empty object "
                    f"of logical stand -> non-empty unit arrays")
            mapping: dict[str, tuple[str, ...]] = {}
            for stand, units in mp_raw.items():
                if not isinstance(stand, str) or not stand.strip():
                    raise StandInputError(
                        "bad_mapping",
                        f"{loc[0]}:{loc[1]}: stand names must be non-empty "
                        f"strings")
                if not isinstance(units, list) or not units:
                    raise StandInputError(
                        "empty_unit_set",
                        f"{loc[0]}:{loc[1]}: stand {stand!r} must map to a "
                        f"non-empty array of physical units")
                tup = []
                for u in units:
                    if not isinstance(u, str) or not u.strip():
                        raise StandInputError(
                            "bad_mapping",
                            f"{loc[0]}:{loc[1]}: unit ids must be non-empty "
                            f"strings (stand {stand!r})")
                    if u not in seen_units:
                        raise StandInputError(
                            "unknown_unit",
                            f"{loc[0]}:{loc[1]}: stand {stand!r} maps to "
                            f"undeclared physical unit {u!r}")
                    if u in tup:
                        raise StandInputError(
                            "duplicate_unit_in_mapping",
                            f"{loc[0]}:{loc[1]}: unit {u!r} listed twice for "
                            f"stand {stand!r}")
                    tup.append(u)
                if stand in mapping:
                    raise StandInputError(
                        "duplicate_stand_in_mapping",
                        f"{loc[0]}:{loc[1]}: stand {stand!r} mapped twice")
                mapping[stand] = tuple(tup)
            inputs.map_versions.append(
                MapVersion(vid, st, en, mapping, sid))
        else:
            raise StandInputError(
                "bad_record_type",
                f"{loc[0]}:{loc[1]}: stand_map lines must be {T_UNIT!r} or "
                f"{T_MAP!r}, got {kind!r}")

    # ---- 4. sensor: online intervals + compact observation blocks
    seen_online: set[str] = set()
    seen_obs: set[str] = set()
    for d, loc, sid in _iter_ndjson(sensor_path, "sensor", src):
        kind = d.get("type")
        unit = _nonempty_str(_require(d, "unit_id", loc), "unit_id", loc)
        if unit not in seen_units:
            raise StandInputError(
                "unknown_unit",
                f"{loc[0]}:{loc[1]}: sensor record refers to undeclared "
                f"physical unit {unit!r}")
        if kind == T_ONLINE:
            rid = loc[2]
            if rid in seen_online:
                raise StandInputError(
                    "duplicate_online_record",
                    f"{loc[0]}:{loc[1]}: online record_id {rid!r} duplicated")
            seen_online.add(rid)
            st = _parse_ts(_require(d, "start", loc), "start", loc)
            en = _parse_ts(_require(d, "end", loc), "end", loc)
            _half_open(st, en, loc)
            inputs.online.append(
                OnlineInterval(rid, unit, _to_us(st.utc), _to_us(en.utc),
                               sid))
        elif kind == T_OBS:
            rid = loc[2]
            if rid in seen_obs:
                raise StandInputError(
                    "duplicate_observation_record",
                    f"{loc[0]}:{loc[1]}: observation record_id {rid!r} "
                    f"duplicated")
            seen_obs.add(rid)
            at = _parse_ts(_require(d, "at", loc), "at", loc)
            state = _require(d, "state", loc)
            if state not in STATE_CODE:
                raise StandInputError(
                    "bad_observation_state",
                    f"{loc[0]}:{loc[1]}: state must be {OBS_IDLE!r} or "
                    f"{OBS_OCCUPIED!r}, got {state!r}")
            blk = inputs.obs_blocks.setdefault(unit, ObsBlock(unit))
            blk.add(_to_us(at.utc), STATE_CODE[state], sid)
        else:
            raise StandInputError(
                "bad_record_type",
                f"{loc[0]}:{loc[1]}: sensor lines must be {T_ONLINE!r} or "
                f"{T_OBS!r}, got {kind!r}")

    for blk in inputs.obs_blocks.values():
        blk.sort()
    # Flush before structural validation: overlap/coverage error details may
    # resolve interned source locations, which must already be in the scratch
    # table (or the in-memory table) rather than stuck in a pending buffer.
    src.flush_scratch()
    _check_online_overlaps(inputs)
    src.flush_scratch()
    return inputs


def _check_online_overlaps(inputs: StandInputs) -> None:
    by_unit: dict[str, list[OnlineInterval]] = {}
    for iv in inputs.online:
        by_unit.setdefault(iv.unit_id, []).append(iv)
    for unit, ivs in by_unit.items():
        ivs.sort(key=lambda x: (x.start_us, x.end_us, x.record_id))
        for prev, nxt in zip(ivs, ivs[1:]):
            if nxt.start_us < prev.end_us:
                p = inputs.sources.resolve(nxt.source_id)
                q = inputs.sources.resolve(prev.source_id)
                raise StandInputError(
                    "overlapping_online_intervals",
                    f"unit {unit!r}: online interval {nxt.record_id!r} "
                    f"({p['file']}:{p['line']}) starts before "
                    f"{prev.record_id!r} ({q['file']}:{q['line']}) ends; "
                    f"online intervals of one unit must not overlap "
                    f"(touching ends are allowed)")


def check_map_covers_window(inputs: StandInputs, win_start_us: int,
                            win_end_us: int) -> None:
    """Map versions must unambiguously (no overlap, no gap) cover the window."""
    if not inputs.map_versions:
        raise StandInputError("no_map_version",
                              "the stand map declares no mapping versions")
    versions = sorted(inputs.map_versions,
                      key=lambda v: (to_us(v.start), to_us(v.end),
                                     v.version_id))
    base_stands = set(versions[0].mapping)
    for v in versions[1:]:
        if set(v.mapping) != base_stands:
            loc = inputs.sources.resolve(v.source_id)
            raise StandInputError(
                "incomplete_map_version",
                f"{loc['file']}:{loc['line']}: map version {v.version_id!r} "
                f"maps {sorted(set(v.mapping))} but the first version maps "
                f"{sorted(base_stands)}; each version must be a complete "
                f"mapping")
    for prev, nxt in zip(versions, versions[1:]):
        if to_us(nxt.start) < to_us(prev.end):
            raise StandInputError(
                "overlapping_map_versions",
                f"map versions {prev.version_id!r} and {nxt.version_id!r} "
                f"overlap; the mapping must be unambiguous at every instant")
        if to_us(nxt.start) > to_us(prev.end):
            raise StandInputError(
                "map_coverage_gap",
                f"no map version covers the gap between "
                f"{prev.end.original} and {nxt.start.original}")
    if to_us(versions[0].start) > win_start_us:
        raise StandInputError(
            "map_coverage_gap",
            f"first map version {versions[0].version_id!r} starts "
            f"{versions[0].start.original!r} after the window start")
    if to_us(versions[-1].end) < win_end_us:
        raise StandInputError(
            "map_coverage_gap",
            f"last map version {versions[-1].version_id!r} ends "
            f"{versions[-1].end.original!r} before the window end")
