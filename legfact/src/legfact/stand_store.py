"""SQLite sidecar for a published stand-occupancy reconciliation.

The sidecar keeps the full time ledger plus every input record needed to
drill from any duration back to evidence: a normalized source-location table,
visits and their fact evidence, raw ledger rows, physical-unit claims with the
map version that produced them, online intervals, observations, segments and
participation issues.

A run writes into a fresh file and atomically renames it into place, so a
failed job (including a structural input rejection) can never leave behind
output that could be mistaken for a complete reconciliation.
"""
from __future__ import annotations

import bisect
import json
import os
import sqlite3
import tempfile

from .stand_engine import SCHEMA, ReconError
from .stand_config import to_us


def _canon(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class ReconBook:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------- writing
    @staticmethod
    def publish(path: str, result: dict, inputs_meta: dict,
                conn: sqlite3.Connection | None = None) -> None:
        """Write ``result`` to ``path`` atomically (temp file + rename)."""
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".recon-", suffix=".sqlite", dir=d)
        os.close(fd)
        try:
            own = conn is None
            if own:
                conn = sqlite3.connect(tmp)
                conn.row_factory = sqlite3.Row
            try:
                if own:
                    conn.executescript(SCHEMA)
                    _populate(conn, result, inputs_meta)
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if row[0] != "ok":  # pragma: no cover
                    raise ReconError("sqlite_integrity", row[0])
                conn.commit()
            finally:
                if own:
                    conn.close()
            os.replace(tmp, os.path.abspath(path))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------- reading
    def header(self) -> dict:
        r = self.conn.execute(
            "SELECT * FROM recon_runs WHERE run_id=1").fetchone()
        if r is None:
            raise ReconError("book_empty", "no reconciliation has been published")
        d = dict(r)
        d["inputs"] = json.loads(d.pop("inputs_json"))
        return d

    def _source(self, source_id: int) -> dict:
        r = self.conn.execute(
            "SELECT file, line, record_id FROM recon_sources "
            "WHERE source_id=?", (source_id,)).fetchone()
        return dict(r) if r is not None else None

    def segments(self, unit: str | None = None) -> list[dict]:
        if unit is None:
            rows = self.conn.execute(
                "SELECT * FROM recon_segments ORDER BY unit_id, start_us")
        else:
            rows = self.conn.execute(
                "SELECT * FROM recon_segments WHERE unit_id=? "
                "ORDER BY start_us", (unit,))
        return [_segment_dict(r) for r in rows]

    def summary(self) -> dict:
        h = self.header()
        n_units = _count_units(self.conn)
        totals: dict[str, int] = {}
        per_unit: dict[str, dict] = {}
        for r in self.conn.execute(
                "SELECT unit_id, recon_class, "
                "SUM(duration_us) AS d, "
                "SUM(CASE WHEN measured_state='occupied' THEN duration_us "
                "    ELSE 0 END) AS lo, "
                "SUM(CASE WHEN measured_state IN ('occupied','unknown') "
                "    THEN duration_us ELSE 0 END) AS hi "
                "FROM recon_segments GROUP BY unit_id, recon_class "
                "ORDER BY unit_id"):
            totals[r["recon_class"]] = totals.get(r["recon_class"], 0) + r["d"]
            pu = per_unit.setdefault(r["unit_id"], {})
            pu[r["recon_class"]] = r["d"]
            pu.setdefault("measured_occupied_us_lower", 0)
            pu.setdefault("measured_occupied_us_upper", 0)
            pu["measured_occupied_us_lower"] += r["lo"]
            pu["measured_occupied_us_upper"] += r["hi"]
        for pu in per_unit.values():
            pu["window_us"] = h["window_us"]
        lower = sum(v["measured_occupied_us_lower"] for v in per_unit.values())
        upper = sum(v["measured_occupied_us_upper"] for v in per_unit.values())
        return {
            "window_us": h["window_us"],
            "unit_time_total_us": h["window_us"] * n_units,
            "class_totals_us": totals,
            "per_unit_us": per_unit,
            "measured_occupied_lower_bound_us": lower,
            "measured_occupied_upper_bound_us": upper,
        }

    def issues(self, kind: str | None = None) -> list[dict]:
        if kind is None:
            rows = self.conn.execute(
                "SELECT * FROM recon_issues ORDER BY issue_id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM recon_issues WHERE kind=? ORDER BY issue_id",
                (kind,))
        return [_issue_dict(r, self) for r in rows]

    def visits(self) -> list[dict]:
        return [_visit_dict(r, self) for r in self.conn.execute(
            "SELECT * FROM recon_visits ORDER BY visit_id")]

    def evidence_at(self, unit: str, at_us: int) -> dict:
        """Everything known about one physical unit at one window instant."""
        h = self.header()
        if not (h["window_start_us"] <= at_us < h["window_end_us"]):
            raise ReconError("outside_window",
                             f"instant {at_us} is outside the audit window")
        seg = self.conn.execute(
            "SELECT * FROM recon_segments WHERE unit_id=? AND start_us<=? "
            "AND end_us>? ORDER BY start_us", (unit, at_us, at_us)).fetchone()
        if seg is None:
            raise ReconError("no_segment",
                             f"unit {unit!r} has no segment at {at_us}")
        seg_d = _segment_dict(seg)
        seg_d["measured_evidence"] = [
            self._source(sid) for sid in seg_d["measured_evidence"]]

        claim_rows = self.conn.execute(
            "SELECT * FROM recon_claims WHERE unit_id=? AND start_us<=? "
            "AND end_us>? ORDER BY claim_id", (unit, at_us, at_us)).fetchall()
        claims = [_claim_dict(r, self) for r in claim_rows]

        visit_ids = {c["visit_id"] for c in claims}
        visit_ids.update(seg_d["conflict_visits"])
        related = []
        for vid in sorted(visit_ids):
            for r in self.conn.execute(
                    "SELECT * FROM recon_claims WHERE visit_id=? AND "
                    "start_us<=? AND end_us>? ORDER BY claim_id",
                    (vid, at_us, at_us)):
                related.append(_claim_dict(r, self))

        visits = []
        for vid in sorted(visit_ids):
            r = self.conn.execute(
                "SELECT * FROM recon_visits WHERE visit_id=?",
                (vid,)).fetchone()
            if r is not None:
                visits.append(_visit_dict(r, self))

        online = self.conn.execute(
            "SELECT * FROM recon_online WHERE unit_id=? AND start_us<=? "
            "AND end_us>? ORDER BY start_us", (unit, at_us, at_us)).fetchall()
        obs = self.conn.execute(
            "SELECT * FROM recon_observations WHERE unit_id=? AND at_us<=? "
            "ORDER BY at_us DESC, record_id LIMIT 8",
            (unit, at_us)).fetchall()
        map_versions = h["inputs"].get("map_versions", [])
        active_map = _map_version_at(map_versions, at_us)
        return {
            "unit_id": unit, "at_us": at_us,
            "segment": seg_d,
            "unit_claims_active": claims,
            "related_visit_declarations": related,
            "visits_and_leg_evidence": visits,
            "active_online_interval": (
                _online_dict(online[0], self) if online else None),
            "recent_observations": [_obs_dict(r, self) for r in obs],
            "map_version": active_map,
            "unknown_reason": seg_d["unknown_reason"],
        }


def _count_units(conn) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT unit_id) FROM recon_segments").fetchone()[0]


def _map_version_at(versions: list[dict], t_us: int):
    for v in versions:
        if v["start_us"] <= t_us < v["end_us"]:
            return v
    return None


def _segment_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["conflict_visits"] = _decode_visits(d.pop("conflict_visits_json"))
    d["measured_evidence"] = _decode_ids(d.pop("measured_evidence_json"))
    d["claim_ids"] = _decode_ids(d.pop("claim_ids_json"))
    return d


def _issue_dict(r: sqlite3.Row, book: ReconBook) -> dict:
    d = dict(r)
    d["source"] = book._source(d.pop("source_id"))
    return d


def _visit_dict(r: sqlite3.Row, book: ReconBook) -> dict:
    d = dict(r)
    d["source"] = book._source(d.pop("source_id"))
    d["arrival_fact_evidence"] = json.loads(d.pop("arr_evidence_json") or "[]")
    d["departure_fact_evidence"] = json.loads(
        d.pop("dep_evidence_json") or "[]")
    return d


def _claim_dict(r: sqlite3.Row, book: ReconBook) -> dict:
    d = dict(r)
    d["source"] = book._source(d.pop("source_id"))
    return d


def _online_dict(r: sqlite3.Row, book: ReconBook) -> dict:
    d = dict(r)
    d["source"] = book._source(d.pop("source_id"))
    return d


def _obs_dict(r: sqlite3.Row, book: ReconBook) -> dict:
    d = dict(r)
    d["source"] = book._source(d.pop("source_id"))
    return d


def _populate(conn: sqlite3.Connection, result: dict, inputs_meta: dict) -> None:
    _populate_header_and_inputs(conn, result, inputs_meta)
    for sg in result["segments"]:
        insert_segment(conn, sg)


def insert_segment(conn: sqlite3.Connection, sg: dict) -> None:
    conn.execute(
        "INSERT INTO recon_segments(unit_id,start_us,end_us,duration_us,"
        "measured_state,unknown_reason,attribution,visit_id,"
        "conflict_visits_json,recon_class,measured_evidence_json,"
        "claim_ids_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sg["unit_id"], sg["start_us"], sg["end_us"], sg["duration_us"],
         sg["measured_state"], sg["unknown_reason"], sg["attribution"],
         sg["visit_id"], _encode_visits(sg["conflict_visits"]),
         sg["recon_class"],
         sg.get("measured_evidence_text")
         or _encode_ids(sg["measured_evidence"]),
         sg.get("claim_ids_text") or _encode_ids(sg["claim_ids"])))


def _encode_ids(values) -> str:
    """Cheap encoding for homogeneous integer id lists (no JSON overhead)."""
    return ",".join(str(v) for v in values)


def _decode_ids(text: str | None) -> list:
    if not text:
        return []
    if text[0] == "[":   # legacy JSON-encoded rows from older sidecars
        return [int(v) for v in json.loads(text)]
    return [int(v) for v in text.split(",")]


def _encode_visits(visits) -> str:
    """Visit ids are short strings; delimit without JSON."""
    return ",".join(visits)


def _decode_visits(text: str | None) -> list:
    if not text:
        return []
    if text[0] == "[":
        return json.loads(text)
    return text.split(",")


class BatchingSegmentSink:
    """Buffers streamed segments and flushes them in one executemany batch.

    Accepts either row dicts (small/test callers) or compact 12-tuples in
    column order (the streaming engine), so the million-row path never builds
    a Python dict per segment.  Buffering bounds live memory to
    ``batch_size`` rows.
    """

    def __init__(self, conn: sqlite3.Connection, batch_size: int = 250,
                 commit_every: int = 50000):
        self.conn = conn
        self.batch_size = batch_size
        # Commit periodically so the rollback journal and dirty pages cannot
        # grow to the size of the whole (multi-hundred-thousand-row) ledger.
        self.commit_every = commit_every
        self.since_commit = 0
        self.buf: list = []
        self.total = 0

    def __call__(self, sg) -> None:
        self.buf.append(sg)
        if len(self.buf) >= self.batch_size:
            self.flush()

    @staticmethod
    def _params(sg):
        if isinstance(sg, tuple):
            return sg
        return (sg["unit_id"], sg["start_us"], sg["end_us"],
                sg["duration_us"], sg["measured_state"],
                sg["unknown_reason"], sg["attribution"], sg["visit_id"],
                _encode_visits(sg["conflict_visits"]), sg["recon_class"],
                sg.get("measured_evidence_text")
                or _encode_ids(sg["measured_evidence"]),
                sg.get("claim_ids_text") or _encode_ids(sg["claim_ids"]))

    def flush(self) -> None:
        if not self.buf:
            return
        rows = [self._params(sg) for sg in self.buf]
        self.conn.executemany(
            "INSERT INTO recon_segments(unit_id,start_us,end_us,duration_us,"
            "measured_state,unknown_reason,attribution,visit_id,"
            "conflict_visits_json,recon_class,measured_evidence_json,"
            "claim_ids_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        n = len(self.buf)
        self.total += n
        self.since_commit += n
        self.buf.clear()
        if self.commit_every and self.since_commit >= self.commit_every:
            self.conn.commit()
            self.since_commit = 0


def _populate_header_and_inputs(conn: sqlite3.Connection, result: dict,
                                inputs_meta: dict) -> None:
    sources = result["sources"]
    same_db_scratch = sources.scratch_conn is conn
    if same_db_scratch:
        # scratch_sources lives in this same database: one set-based copy.
        conn.execute(
            "INSERT INTO recon_sources(source_id,file,line,record_id) "
            "SELECT source_id,file,line,record_id FROM scratch_sources "
            "ORDER BY source_id")
    else:
        conn.executemany(
            "INSERT INTO recon_sources(source_id,file,line,record_id) "
            "VALUES(?,?,?,?)",
            ((sid, loc["file"], loc["line"], loc["record_id"])
             for sid, loc in sources.rows()))

    w = result["window"]
    map_sources = None
    if same_db_scratch:
        map_sources = {r[0]: {"file": r[1], "line": r[2], "record_id": r[3]}
                       for r in conn.execute(
                           "SELECT source_id,file,line,record_id "
                           "FROM scratch_sources")}
    inputs_json = _canon({
        "files": inputs_meta["files"],
        "map_versions": [
            {"version_id": v.version_id,
             "start_us": to_us(v.start), "end_us": to_us(v.end),
             "mapping": {k: list(us) for k, us in v.mapping.items()},
             "source": (map_sources[v.source_id] if same_db_scratch
                        else sources.resolve(v.source_id))}
            for v in result["map_versions"]],
        "sha256": inputs_meta.get("sha256", {}),
    })
    conn.execute(
        "INSERT INTO recon_runs(run_id,airport,window_start_utc,"
        "window_end_utc,window_start_us,window_end_us,window_us,"
        "receipt_batch,cutoff_text,cutoff_utc,inputs_json,created_at) "
        "VALUES(1,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (result["airport"], w["start"], w["end"], result["win_start_us"],
         result["win_end_us"], w["duration_us"], result["receipt_batch"],
         result["cutoff"]["text"], result["cutoff"]["utc"], inputs_json))

    conn.executemany(
        "INSERT INTO recon_visits(visit_id,status,arrival_root,"
        "departure_root,ground_start_us,ground_end_us,reason,"
        "reason_detail,source_id,arr_evidence_json,dep_evidence_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ((vo.visit_id, vo.status, vo.arrival_root, vo.departure_root,
          vo.ground_start, vo.ground_end, vo.reason, vo.reason_detail,
          vo.source_id, _canon(vo.arr_evidence), _canon(vo.dep_evidence))
         for vo in result["visits"]))

    conn.executemany(
        "INSERT INTO recon_ledger(record_id,visit_id,stand,start_us,"
        "end_us,source_id,usable,issue_kind) VALUES(?,?,?,?,?,?,?,?)",
        result["stored_ledger_rows"])

    conn.executemany(
        "INSERT INTO recon_claims(claim_id,ledger_record_id,visit_id,"
        "stand,unit_id,start_us,end_us,map_version,source_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ((c.claim_id, c.ledger_record_id, c.visit_id, c.stand, c.unit_id,
          c.start, c.end, c.map_version, c.source_id)
         for c in result["claims"]))

    conn.executemany(
        "INSERT INTO recon_online(record_id,unit_id,start_us,end_us,"
        "source_id) VALUES(?,?,?,?,?)",
        ((iv.record_id, iv.unit_id, iv.start_us, iv.end_us, iv.source_id)
         for iv in result["online_rows"]))

    # Observations live in compact per-unit blocks.  Stage the integer columns
    # first (no per-row source resolution in Python) and resolve record ids
    # with one set-based join against the source table — this is what makes a
    # million-row load cheap in scratch mode.
    conn.execute(
        "CREATE TEMP TABLE tmp_obs_stage(unit_id TEXT, at_us INTEGER, "
        "state_code INTEGER, in_online INTEGER, source_id INTEGER)")
    online_sorted: dict[str, list] = {}
    for iv in result["online_rows"]:
        online_sorted.setdefault(iv.unit_id, []).append(
            (iv.start_us, iv.end_us))
    for ivs in online_sorted.values():
        ivs.sort()
    # Stream stage rows in batches so the million-observation input never
    # materializes as a million live Python tuples.
    stage_buf: list[tuple] = []
    for unit, blk in result["obs_blocks"].items():
        ivs = online_sorted.get(unit, [])
        starts = [lo for lo, _hi in ivs]
        for i, at_us in enumerate(blk.at):
            packed = int(blk.packed[i])
            state_code, sid = packed & 1, packed >> 1
            j = bisect.bisect_right(starts, at_us) - 1
            in_online = int(j >= 0 and at_us < ivs[j][1])
            stage_buf.append((unit, at_us, state_code, in_online, sid))
            if len(stage_buf) >= 20000:
                conn.executemany(
                    "INSERT INTO tmp_obs_stage VALUES(?,?,?,?,?)", stage_buf)
                stage_buf.clear()
    if stage_buf:
        conn.executemany(
            "INSERT INTO tmp_obs_stage VALUES(?,?,?,?,?)", stage_buf)
        stage_buf.clear()
    conn.execute(
        "INSERT INTO recon_observations(record_id,unit_id,at_us,state,"
        "in_online,source_id) "
        "SELECT s.record_id, st.unit_id, st.at_us, "
        "CASE st.state_code WHEN 1 THEN 'occupied' ELSE 'idle' END, "
        "st.in_online, st.source_id "
        "FROM tmp_obs_stage st JOIN recon_sources s "
        "ON s.source_id=st.source_id")
    conn.execute("DROP TABLE tmp_obs_stage")

    conn.executemany(
        "INSERT INTO recon_issues(kind,detail,source_id,visit_id,"
        "ledger_record_id,unit_id) VALUES(?,?,?,?,?,?)",
        ((iss.kind, iss.detail, iss.source_id, iss.visit_id,
          iss.ledger_record_id, iss.unit_id) for iss in result["issues"]))
