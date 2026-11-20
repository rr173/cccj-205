"""Persistent knowledge-batch storage.

Layout under a warehouse directory::

    warehouse/
      legfact.sqlite          # all index/projection data (standard-library sqlite3)
      blobs/<batch>/<file>    # verbatim received files, never rewritten
      manifest/<batch>.json   # accepted manifest copy
      tmp/                    # staging area of an open (unpublished) batch

Batches move through ``open -> published`` (or stay ``open`` after an
interrupted/aborted ingest).  Only published batches are visible to queries;
an unfinished batch can never partially overwrite older knowledge because all
of its writes live in the same transaction committed at publish time.
Copying the whole directory and reopening it gives a queryable replica of
every old batch.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from . import aidx, identity
from .aidx import ParsedMessage
from .timeutil import parse_offset_datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  batch_id        INTEGER PRIMARY KEY,
  status          TEXT NOT NULL CHECK (status IN ('open','published','aborted')),
  sender          TEXT NOT NULL,
  opened_at       TEXT NOT NULL,
  published_at    TEXT
);
CREATE TABLE IF NOT EXISTS batch_files (
  batch_id        INTEGER NOT NULL REFERENCES batches(batch_id),
  rel_path        TEXT NOT NULL,
  sha256          TEXT NOT NULL,
  size_bytes      INTEGER NOT NULL,
  kind            TEXT NOT NULL,
  PRIMARY KEY (batch_id, rel_path)
);
CREATE TABLE IF NOT EXISTS events (
  event_id        TEXT NOT NULL,
  batch_id        INTEGER NOT NULL REFERENCES batches(batch_id),
  sender          TEXT NOT NULL,
  source_revision INTEGER NOT NULL,
  effective_at    TEXT NOT NULL,
  identity_at     TEXT NOT NULL,
  message_type    TEXT NOT NULL,
  is_authoritative INTEGER NOT NULL,
  binding_status  TEXT NOT NULL,
  root_id         TEXT,
  raw_ref         TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  conflict_with   TEXT,
  PRIMARY KEY (event_id, batch_id)
);
CREATE TABLE IF NOT EXISTS event_keys (
  event_id        TEXT NOT NULL,
  batch_id        INTEGER NOT NULL,
  carrier         TEXT NOT NULL,
  flight_no       TEXT NOT NULL,
  suffix          TEXT NOT NULL,
  service_date    TEXT NOT NULL,
  departure       TEXT NOT NULL,
  arrival         TEXT NOT NULL,
  PRIMARY KEY (event_id, batch_id)
);
CREATE TABLE IF NOT EXISTS event_fields (
  event_id        TEXT NOT NULL,
  batch_id        INTEGER NOT NULL,
  field           TEXT NOT NULL,
  state           TEXT NOT NULL,
  value           TEXT,
  xml_location    TEXT NOT NULL,
  line            INTEGER NOT NULL,
  columnn         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_cancellations (
  event_id        TEXT NOT NULL,
  batch_id        INTEGER NOT NULL,
  scope           TEXT NOT NULL,
  action          TEXT NOT NULL,
  journey_id      TEXT,
  xml_location    TEXT NOT NULL,
  line            INTEGER NOT NULL,
  columnn         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_unsupported (
  event_id        TEXT NOT NULL,
  batch_id        INTEGER NOT NULL,
  kind            TEXT NOT NULL,
  name            TEXT NOT NULL,
  xml_location    TEXT NOT NULL,
  detail          TEXT
);
CREATE TABLE IF NOT EXISTS event_parse_errors (
  event_id        TEXT,
  batch_id        INTEGER NOT NULL,
  raw_ref         TEXT NOT NULL,
  error           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_relations (
  batch_id        INTEGER NOT NULL,
  rel_seq         INTEGER NOT NULL,
  rel_id          TEXT NOT NULL,
  rel_type        TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  PRIMARY KEY (batch_id, rel_seq)
);
CREATE TABLE IF NOT EXISTS identity_warnings (
  batch_id        INTEGER NOT NULL,
  code            TEXT NOT NULL,
  detail          TEXT NOT NULL,
  relations_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_evidence (
  batch_id        INTEGER NOT NULL,
  event_id        TEXT NOT NULL,
  sender          TEXT NOT NULL,
  reason          TEXT NOT NULL,
  raw_ref         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_root ON events(root_id, effective_at);
CREATE INDEX IF NOT EXISTS idx_fields ON event_fields(field, batch_id);
CREATE INDEX IF NOT EXISTS idx_ek ON event_keys(batch_id);
CREATE INDEX IF NOT EXISTS idx_irel ON identity_relations(batch_id);
"""


class IngestError(Exception):
    pass


@dataclass
class IngestReport:
    batch_id: int
    sender: str
    files: list[dict]
    events_accepted: int
    events_retransmitted: int
    events_conflicting: int
    events_quarantined: int
    external_evidence: int
    parse_errors: list[dict]
    unsupported: list[dict]
    identity_relations: int
    identity_warnings: list[dict]
    published: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


class Warehouse:
    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        os.makedirs(self.root_dir, exist_ok=True)
        self.blobs_dir = os.path.join(self.root_dir, "blobs")
        self.manifest_dir = os.path.join(self.root_dir, "manifest")
        self.tmp_dir = os.path.join(self.root_dir, "tmp")
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.manifest_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.root_dir, "legfact.sqlite")
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self.profile = aidx.load_profile()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def blob_path(self, batch_id: int, rel_path: str) -> str:
        return os.path.join(self.blobs_dir, str(batch_id), os.path.basename(rel_path))

    # ------------------------------------------------------------ batch meta
    def list_batches(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM batches ORDER BY batch_id"))

    def published_batches(self) -> list[int]:
        return [r["batch_id"] for r in self._conn.execute(
            "SELECT status, batch_id FROM batches ORDER BY batch_id")
            if r["status"] == "published"]

    def _open_batch_id(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT batch_id FROM batches WHERE status='open' "
            "ORDER BY batch_id DESC LIMIT 1").fetchone()
        return row["batch_id"] if row else None

    def require_published(self, batch_id: int) -> None:
        row = self._conn.execute(
            "SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise IngestError(f"receipt_batch {batch_id} does not exist")
        if row["status"] != "published":
            raise IngestError(
                f"receipt_batch {batch_id} is {row['status']}, not published; "
                f"unfinished batches are never queryable")

    # -------------------------------------------------------------- ingest
    def ingest_batch(self, manifest_path: str, files_root: str,
                     publish: bool = True) -> IngestReport:
        with open(manifest_path, "rb") as fh:
            manifest_bytes = fh.read()
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise IngestError(f"manifest is not valid JSON: {exc}") from exc
        self._validate_manifest_shape(manifest)

        if self._open_batch_id() is not None:
            raise IngestError(
                "an open (unfinished) batch already exists; abort or publish it first")
        prev = self._conn.execute(
            "SELECT COALESCE(MAX(batch_id),0) AS m FROM batches").fetchone()["m"]
        if manifest["receipt_batch"] != prev + 1:
            raise IngestError(
                f"receipt_batch {manifest['receipt_batch']} breaks continuous "
                f"sequence; next expected {prev + 1}")
        if not manifest["sender"]:
            raise IngestError("manifest sender must not be empty")

        cur = self._conn.execute(
            "INSERT INTO batches(status,sender,opened_at,published_at) "
            "VALUES('open',?,datetime('now'),NULL)", (manifest["sender"],))
        batch_id = cur.lastrowid
        batch_blob_dir = os.path.join(self.blobs_dir, str(batch_id))
        os.makedirs(batch_blob_dir, exist_ok=True)

        report = IngestReport(
            batch_id=batch_id, sender=manifest["sender"], files=[],
            events_accepted=0, events_retransmitted=0, events_conflicting=0,
            events_quarantined=0, external_evidence=0, parse_errors=[],
            unsupported=[], identity_relations=0, identity_warnings=[],
            published=False,
        )
        try:
            with self.transaction() as conn:
                self._ingest_files(conn, batch_id, manifest, files_root,
                                   batch_blob_dir, report)
                graph, warns = self._materialize_identity(conn, batch_id)
                report.identity_warnings = [
                    {"code": w.code, "detail": w.detail, "relations": w.relations}
                    for w in warns]
                self._bind_events(conn, batch_id, graph, report)
                if publish:
                    conn.execute(
                        "UPDATE batches SET status='published', "
                        "published_at=datetime('now') WHERE batch_id=?",
                        (batch_id,))
                    report.published = True
            with open(os.path.join(self.manifest_dir, f"{batch_id}.json"), "wb") as fh:
                fh.write(manifest_bytes)
        except Exception:
            self._conn.rollback()
            raise
        return report

    def abort_open_batch(self) -> Optional[int]:
        bid = self._open_batch_id()
        if bid is None:
            return None
        with self.transaction() as conn:
            conn.execute("UPDATE batches SET status='aborted' WHERE batch_id=?", (bid,))
        return bid

    def publish_open_batch(self) -> int:
        bid = self._open_batch_id()
        if bid is None:
            raise IngestError("no open batch to publish")
        with self.transaction() as conn:
            graph, _ = self._materialize_identity(conn, bid)
            self._rebind_forward(conn, graph)
            conn.execute(
                "UPDATE batches SET status='published', published_at=datetime('now') "
                "WHERE batch_id=?", (bid,))
        return bid

    # ----------------------------------------------------- manifest helpers
    @staticmethod
    def _validate_manifest_shape(m: dict) -> None:
        for k in ("sender", "receipt_batch", "files"):
            if k not in m:
                raise IngestError(f"manifest missing required key {k!r}")
        if not isinstance(m["receipt_batch"], int) or m["receipt_batch"] <= 0:
            raise IngestError("manifest receipt_batch must be a positive integer")
        if not isinstance(m["files"], list):
            raise IngestError("manifest files must be an array")
        for f in m["files"]:
            for k in ("path", "sha256", "size_bytes", "kind"):
                if k not in f:
                    raise IngestError(f"manifest file entry missing {k!r}: {f}")
            if f["kind"] not in ("aidx_xml", "identity_json"):
                raise IngestError(f"unknown file kind {f['kind']!r}")

    def _ingest_files(self, conn, batch_id: int, manifest: dict,
                      files_root: str, blob_dir: str, report: IngestReport) -> None:
        seen_paths: set[str] = set()
        identity_records: list[tuple[int, dict]] = []
        for entry in manifest["files"]:
            rel = entry["path"]
            if rel in seen_paths:
                raise IngestError(f"duplicate file path in manifest: {rel}")
            seen_paths.add(rel)
            src = os.path.join(files_root, rel)
            if not os.path.isfile(src):
                raise IngestError(f"manifest file missing on disk: {rel}")
            digest, size = sha256_file(src)
            if digest != entry["sha256"]:
                raise IngestError(f"sha256 mismatch for {rel}")
            if size != entry["size_bytes"]:
                raise IngestError(f"size mismatch for {rel}")
            dst = os.path.join(blob_dir, os.path.basename(rel))
            with open(src, "rb") as fh, open(dst, "wb") as out:
                out.write(fh.read())
            conn.execute(
                "INSERT INTO batch_files(batch_id,rel_path,sha256,size_bytes,kind) "
                "VALUES(?,?,?,?,?)",
                (batch_id, rel, digest, size, entry["kind"]))
            report.files.append({"path": rel, "sha256": digest, "size_bytes": size,
                                 "kind": entry["kind"]})
            if entry["kind"] == "identity_json":
                with open(src, "rb") as fh:
                    doc = json.loads(fh.read().decode("utf-8"))
                for seq, rec in enumerate(doc.get("relations", [])):
                    identity_records.append((seq, rec))
            else:
                self._ingest_aidx(conn, batch_id, rel, dst, report)

        for seq, rec in identity_records:
            conn.execute(
                "INSERT INTO identity_relations(batch_id,rel_seq,rel_id,rel_type,payload_json) "
                "VALUES(?,?,?,?,?)",
                (batch_id, seq, rec.get("id", f"rel#{seq}"), rec["type"],
                 json.dumps(rec, ensure_ascii=False, sort_keys=True)))
        report.identity_relations = len(identity_records)

    def _ingest_aidx(self, conn, batch_id: int, rel: str, blob_path: str,
                     report: IngestReport) -> None:
        with open(blob_path, "rb") as fh:
            data = fh.read()
        pm: ParsedMessage = aidx.parse_aidx(data, self.profile, raw_ref=rel)

        if pm.parse_error is not None:
            conn.execute(
                "INSERT INTO event_parse_errors(event_id,batch_id,raw_ref,error) "
                "VALUES(?,?,?,?)",
                (pm.event_id or None, batch_id, rel, pm.parse_error))
            report.parse_errors.append({"file": rel, "error": pm.parse_error})
            report.events_quarantined += 1
            return
        if not pm.projectable:
            for iss in pm.unsupported:
                conn.execute(
                    "INSERT INTO event_unsupported(event_id,batch_id,kind,name,xml_location,detail) "
                    "VALUES(?,?,?,?,?,?)",
                    (pm.event_id, batch_id, iss.kind, iss.name, iss.xpath, iss.detail))
                report.unsupported.append({"file": rel, "kind": iss.kind,
                                           "name": iss.name, "xpath": iss.xpath})
            conn.execute(
                "INSERT INTO event_parse_errors(event_id,batch_id,raw_ref,error) "
                "VALUES(?,?,?,?)",
                (pm.event_id, batch_id, rel,
                 "quarantined: unsupported profile elements present"))
            report.events_quarantined += 1
            return

        payload_hash = hashlib.sha256(
            json.dumps(self._event_payload(pm), sort_keys=True).encode("utf-8")
        ).hexdigest()
        prior = conn.execute(
            "SELECT payload_hash FROM events WHERE event_id=? "
            "ORDER BY batch_id DESC LIMIT 1", (pm.event_id,)).fetchone()
        conflict_with = None
        if prior is not None:
            if prior["payload_hash"] == payload_hash:
                report.events_retransmitted += 1
            else:
                conflict_with = pm.event_id
                report.events_conflicting += 1
        else:
            report.events_accepted += 1

        conn.execute(
            "INSERT INTO events(event_id,batch_id,sender,source_revision,"
            "effective_at,identity_at,message_type,is_authoritative,binding_status,"
            "root_id,raw_ref,payload_hash,conflict_with) "
            "VALUES(?,?,?,?,?,?,?,0,'forward',NULL,?,?,?)",
            (pm.event_id, batch_id, pm.sender, pm.source_revision,
             pm.effective_at.iso_utc(), pm.identity_at.iso_utc(), pm.message_type,
             rel, payload_hash, conflict_with))
        k = pm.operating_key
        conn.execute(
            "INSERT INTO event_keys(event_id,batch_id,carrier,flight_no,suffix,"
            "service_date,departure,arrival) VALUES(?,?,?,?,?,?,?,?)",
            (pm.event_id, batch_id, k.carrier, k.flight_no, k.suffix,
             k.service_date, k.departure, k.arrival))
        for fv in pm.fields:
            conn.execute(
                "INSERT INTO event_fields(event_id,batch_id,field,state,value,"
                "xml_location,line,columnn) VALUES(?,?,?,?,?,?,?,?)",
                (pm.event_id, batch_id, fv.field, fv.state, fv.value,
                 fv.location, fv.line, fv.column))
        if pm.cancellation is not None:
            c = pm.cancellation
            conn.execute(
                "INSERT INTO event_cancellations(event_id,batch_id,scope,action,"
                "journey_id,xml_location,line,columnn) VALUES(?,?,?,?,?,?,?,?)",
                (pm.event_id, batch_id, c.scope, c.action, c.journey_id,
                 c.location, c.line, c.column))

    @staticmethod
    def _event_payload(pm: ParsedMessage) -> dict:
        return {
            "sender": pm.sender, "source_revision": pm.source_revision,
            "effective_at": pm.effective_at.original,
            "identity_at": pm.identity_at.original,
            "key": pm.operating_key.to_dict(),
            "fields": sorted((f.field, f.state, f.value) for f in pm.fields),
            "cancellation": None if pm.cancellation is None
            else (pm.cancellation.scope, pm.cancellation.action,
                  pm.cancellation.journey_id),
        }

    # ------------------------------------------------------------ identity
    def _load_relations_through(self, conn, batch_id: int):
        rows = conn.execute(
            "SELECT payload_json FROM identity_relations WHERE batch_id<=? "
            "ORDER BY batch_id, rel_seq", (batch_id,)).fetchall()
        doc = {"relations": [json.loads(r["payload_json"]) for r in rows]}
        return identity.parse_identity_file(doc)

    def _materialize_identity(self, conn, batch_id: int):
        initials, renames, codeshares = self._load_relations_through(conn, batch_id)
        graph = identity.IdentityGraph.build(initials, renames, codeshares)
        conn.execute("DELETE FROM identity_warnings WHERE batch_id=?", (batch_id,))
        for w in graph.warnings:
            conn.execute(
                "INSERT INTO identity_warnings(batch_id,code,detail,relations_json) "
                "VALUES(?,?,?,?)",
                (batch_id, w.code, w.detail,
                 json.dumps(w.relations, ensure_ascii=False)))
        return graph, graph.warnings

    def load_graph(self, batch_id: int) -> identity.IdentityGraph:
        self.require_published(batch_id)
        initials, renames, codeshares = self._load_relations_through(
            self._conn, batch_id)
        return identity.IdentityGraph.build(initials, renames, codeshares)

    def _bind_events(self, conn, batch_id: int, graph: identity.IdentityGraph,
                     report: IngestReport) -> None:
        rows = conn.execute(
            "SELECT e.*, k.carrier AS k_carrier, k.flight_no AS k_fno, k.suffix AS k_sfx, "
            "k.service_date AS k_sd, k.departure AS k_dep, k.arrival AS k_arr "
            "FROM events e JOIN event_keys k USING(event_id,batch_id) "
            "WHERE e.batch_id=?", (batch_id,)).fetchall()
        for row in rows:
            pm_key = identity.OpKey(
                row["k_carrier"], row["k_fno"], row["k_sfx"], row["k_sd"],
                row["k_dep"], row["k_arr"], "OP")
            ident_at = parse_offset_datetime(row["identity_at"])
            binding = graph.bind_operating(pm_key, ident_at)
            is_auth = 0
            # Only a definite single-root binding can be authoritative and
            # project facts. Ambiguous messages are retained on the candidate
            # root when one is obvious, but never counted as authoritative.
            root_for_row = binding.root_id
            if binding.status == "bound":
                is_auth = int(binding.sender == row["sender"])
                if not is_auth:
                    conn.execute(
                        "INSERT INTO external_evidence(batch_id,event_id,sender,reason,raw_ref) "
                        "VALUES(?,?,?,?,?)",
                        (batch_id, row["event_id"], row["sender"],
                         f"sender {row['sender']!r} is not the authoritative sender "
                         f"{binding.sender!r} of root {binding.root_id}", row["raw_ref"]))
                    report.external_evidence += 1
            elif binding.status == "ambiguous" and binding.root_id:
                conn.execute(
                    "INSERT INTO external_evidence(batch_id,event_id,sender,reason,raw_ref) "
                    "VALUES(?,?,?,?,?)",
                    (batch_id, row["event_id"], row["sender"],
                     f"identity ambiguity {binding.candidate_roots}; message retained "
                     f"on candidate root {binding.root_id} but not projected as fact",
                     row["raw_ref"]))
                report.external_evidence += 1
            conn.execute(
                "UPDATE events SET binding_status=?, root_id=?, is_authoritative=? "
                "WHERE event_id=? AND batch_id=?",
                (binding.status, root_for_row, is_auth,
                 row["event_id"], batch_id))

    def _rebind_forward(self, conn, graph: identity.IdentityGraph) -> None:
        rows = conn.execute(
            "SELECT e.*, k.carrier AS k_carrier, k.flight_no AS k_fno, k.suffix AS k_sfx, "
            "k.service_date AS k_sd, k.departure AS k_dep, k.arrival AS k_arr "
            "FROM events e JOIN event_keys k USING(event_id,batch_id) "
            "WHERE e.binding_status IN ('forward','unknown')").fetchall()
        for row in rows:
            pm_key = identity.OpKey(
                row["k_carrier"], row["k_fno"], row["k_sfx"], row["k_sd"],
                row["k_dep"], row["k_arr"], "OP")
            ident_at = parse_offset_datetime(row["identity_at"])
            binding = graph.bind_operating(pm_key, ident_at)
            is_auth = int(binding.status == "bound" and binding.sender == row["sender"])
            conn.execute(
                "UPDATE events SET binding_status=?, root_id=?, is_authoritative=? "
                "WHERE event_id=? AND batch_id=?",
                (binding.status, binding.root_id, is_auth,
                 row["event_id"], row["batch_id"]))
