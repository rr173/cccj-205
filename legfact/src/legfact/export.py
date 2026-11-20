"""Source message export and identity explanation.

Everything a fact result cites can be exported back to its original evidence:
the verbatim XML blob, the manifest entry, the identity chain and the
ingest-time warnings.  Exports never reconstruct XML by string substitution;
the stored blob is returned byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from . import identity
from .storage import Warehouse
from .timeutil import parse_offset_datetime


class ExportError(ValueError):
    pass


@dataclass
class ExportedMessage:
    event_id: str
    receipt_batch: int
    raw_rel_path: str
    raw_xml: str
    sha256: str
    manifest_entry: dict
    envelope: dict
    fields: list[dict]
    cancellations: list[dict]
    unsupported: list[dict]
    binding: dict


class Exporter:
    def __init__(self, warehouse: Warehouse):
        self.wh = warehouse

    def _blob(self, batch_id: int, rel_path: str) -> bytes:
        path = self.wh.blob_path(batch_id, rel_path)
        if not os.path.isfile(path):
            raise ExportError(f"blob missing for {rel_path} (batch {batch_id})")
        with open(path, "rb") as fh:
            return fh.read()

    def manifest(self, batch_id: int) -> dict:
        self.wh.require_published(batch_id)
        mpath = os.path.join(self.wh.manifest_dir, f"{batch_id}.json")
        with open(mpath, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def export_event(self, event_id: str, receipt_batch: Optional[int] = None) -> ExportedMessage:
        conn = self.wh._conn
        if receipt_batch is None:
            row = conn.execute(
                "SELECT * FROM events WHERE event_id=? ORDER BY batch_id LIMIT 1",
                (event_id,)).fetchone()
        else:
            self.wh.require_published(receipt_batch)
            row = conn.execute(
                "SELECT * FROM events WHERE event_id=? AND batch_id=?",
                (event_id, receipt_batch)).fetchone()
        if row is None:
            raise ExportError(f"event {event_id!r} not found"
                              + (f" in batch {receipt_batch}" if receipt_batch else ""))
        bid = row["batch_id"]
        raw = self._blob(bid, row["raw_ref"])
        manifest = self.manifest(bid)
        m_entry = next((f for f in manifest["files"] if f["path"] == row["raw_ref"]), {})
        fields = [dict(r) for r in conn.execute(
            "SELECT field,state,value,xml_location,line,columnn AS column FROM event_fields "
            "WHERE event_id=? AND batch_id=? ORDER BY field", (event_id, bid))]
        cancels = [dict(r) for r in conn.execute(
            "SELECT scope,action,journey_id,xml_location,line,columnn AS column "
            "FROM event_cancellations WHERE event_id=? AND batch_id=?",
            (event_id, bid))]
        unsupported = [dict(r) for r in conn.execute(
            "SELECT kind,name,xml_location,detail FROM event_unsupported "
            "WHERE event_id=? AND batch_id=?", (event_id, bid))]
        key_row = conn.execute(
            "SELECT * FROM event_keys WHERE event_id=? AND batch_id=?",
            (event_id, bid)).fetchone()
        return ExportedMessage(
            event_id=event_id, receipt_batch=bid, raw_rel_path=row["raw_ref"],
            raw_xml=raw.decode("utf-8"),
            sha256=hashlib.sha256(raw).hexdigest(),
            manifest_entry=m_entry,
            envelope={
                "event_id": row["event_id"], "sender": row["sender"],
                "source_revision": row["source_revision"],
                "effective_at": row["effective_at"],
                "identity_at": row["identity_at"],
                "message_type": row["message_type"],
                "operating_key": dict(key_row) if key_row else None,
                "is_authoritative": bool(row["is_authoritative"]),
                "binding_status": row["binding_status"],
                "root_id": row["root_id"],
                "payload_conflict_with": row["conflict_with"],
            },
            fields=fields, cancellations=cancels, unsupported=unsupported,
            binding={"status": row["binding_status"], "root_id": row["root_id"]})

    def explain_identity(self, key: identity.OpKey, receipt_batch: int,
                         identity_at: str) -> dict:
        """Return the full identity association chain for a key at an instant."""
        self.wh.require_published(receipt_batch)
        graph = self.wh.load_graph(receipt_batch)
        at = parse_offset_datetime(identity_at)
        if key.kind == "MK":
            binding = graph.bind_marketing(key, at)
        else:
            binding = graph.bind_operating(key, at)
        snapshot_warnings = [
            {"code": r["code"], "detail": r["detail"],
             "relations": json.loads(r["relations_json"])}
            for r in self.wh._conn.execute(
                "SELECT * FROM identity_warnings WHERE batch_id=?",
                (receipt_batch,)).fetchall()]
        return {
            "key": key.to_dict(),
            "key_kind": "marketing" if key.kind == "MK" else "operating",
            "identity_at": at.iso_utc(),
            "receipt_batch": receipt_batch,
            "status": binding.status,
            "root_id": binding.root_id,
            "journey_id": binding.journey_id,
            "authoritative_sender": binding.sender,
            "association_chain": binding.chain,
            "candidate_roots": binding.candidate_roots,
            "snapshot_identity_warnings": snapshot_warnings,
        }

    def batch_ingest_warnings(self, batch_id: int) -> list[dict]:
        self.wh.require_published(batch_id)
        return [dict(r) for r in self.wh._conn.execute(
            "SELECT code,detail,relations_json FROM identity_warnings "
            "WHERE batch_id=? ORDER BY code", (batch_id,))]

    def parse_errors(self, batch_id: int) -> list[dict]:
        return [dict(r) for r in self.wh._conn.execute(
            "SELECT event_id,raw_ref,error FROM event_parse_errors WHERE batch_id=?",
            (batch_id,))]
