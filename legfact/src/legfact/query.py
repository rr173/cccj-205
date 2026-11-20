"""Replay-based leg-fact query engine.

A query fixes TWO time cutoffs in a single consistent read:

1. ``receipt_batch`` — the knowledge cut: only messages and identity
   relations known in that published batch are visible; later batches can
   never rewrite old knowledge.
2. ``business_time`` — the business instant: only messages whose
   ``effective_at <= business_time`` are replayed.

For each fact field independently we select the update with the greatest
``(effective_at, source_revision)`` (revisions are only compared within the
same sender, and every leg has exactly one authoritative sender, so external
evidence never participates in field projection).  Equal priority coordinates
with different values yield a field-level ``conflict`` — no value is picked
and the conflict never spreads to unrelated fields.  An explicit clear
overwrites an older value and is reported as ``cleared``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from . import identity
from .storage import Warehouse
from .timeutil import OffsetDateTime, parse_offset_datetime

FACT_FIELDS = (
    "sched_dep", "sched_arr",
    "est_dep", "est_arr",
    "act_dep", "act_arr",
    "dep_gate", "arr_gate",
)
FIELD_LABELS = {
    "sched_dep": "scheduled_departure", "sched_arr": "scheduled_arrival",
    "est_dep": "estimated_departure", "est_arr": "estimated_arrival",
    "act_dep": "actual_departure", "act_arr": "actual_arrival",
    "dep_gate": "departure_gate", "arr_gate": "arrival_gate",
}


@dataclass
class FieldResult:
    field: str
    label: str
    state: str                  # value | absent | cleared | conflict
    value: Optional[str] = None
    contenders: list[dict] = field(default_factory=list)
    winning: Optional[dict] = None


@dataclass
class CancellationResult:
    state: str                  # cancelled | restored | absent | conflict
    actions: list[dict] = field(default_factory=list)


@dataclass
class LegFactResult:
    queried_key: dict
    key_kind: str               # OP | MK
    binding_status: str
    root_id: Optional[str]
    journey_id: Optional[str]
    authoritative_sender: Optional[str]
    identity_chain: list[dict]
    fields: dict[str, FieldResult]
    cancellation: CancellationResult
    warnings: list[dict]
    query: dict
    external_evidence: list[dict] = field(default_factory=list)
    candidate_roots: list[str] = field(default_factory=list)


class QueryError(ValueError):
    pass


class QueryEngine:
    def __init__(self, warehouse: Warehouse):
        self.wh = warehouse

    # ---------------------------------------------------------- public API
    def query(self, key: identity.OpKey, receipt_batch: int,
              business_time: str) -> LegFactResult:
        self.wh.require_published(receipt_batch)
        bt = parse_offset_datetime(business_time)
        graph = self.wh.load_graph(receipt_batch)

        if key.kind == "MK":
            binding = graph.bind_marketing(key, bt)
        else:
            binding = graph.bind_operating(key, bt)

        chain = list(binding.chain)
        warnings = [{"code": w.code, "detail": w.detail, "relations": w.relations}
                    for w in binding.warnings]

        if binding.status != "bound":
            conn = self.wh._conn
            # Ambiguous keys still surface the candidate roots and any
            # evidence retained on them; nothing is projected as a definite
            # fact because we refuse to guess an edge.
            candidate_evidence: list[dict] = []
            if binding.status == "ambiguous":
                for cand in binding.candidate_roots or (
                        [binding.root_id] if binding.root_id else []):
                    candidate_evidence.extend(
                        self._external(conn, graph, cand, receipt_batch, bt))
            return LegFactResult(
                queried_key=key.to_dict(), key_kind=key.kind,
                binding_status=binding.status, root_id=binding.root_id,
                journey_id=binding.journey_id,
                authoritative_sender=binding.sender, identity_chain=chain,
                fields={f: FieldResult(f, FIELD_LABELS[f], "absent")
                        for f in FACT_FIELDS},
                cancellation=CancellationResult("absent"),
                warnings=warnings,
                query={"receipt_batch": receipt_batch,
                       "business_time": bt.iso_utc()},
                external_evidence=candidate_evidence,
                candidate_roots=list(binding.candidate_roots or []))

        root_id = binding.root_id
        il = graph.initials[root_id]
        conn = self.wh._conn
        # single consistent read: snapshot via one connection, no writes.
        # Events are rebound at QUERY time against the identity graph of the
        # requested knowledge cut, so forward references that closed in a
        # later (still <= receipt_batch) batch resolve correctly, and stale
        # ingest-time binding labels never decide fact projection.
        events = self._root_events(conn, graph, root_id, receipt_batch, bt)
        field_results = self._resolve_fields(conn, events, receipt_batch)
        cancel = self._resolve_cancellation(conn, graph, events, root_id,
                                            receipt_batch, bt)
        ext = self._external(conn, graph, root_id, receipt_batch, bt)
        return LegFactResult(
            queried_key=key.to_dict(), key_kind=key.kind,
            binding_status="bound", root_id=root_id, journey_id=il.journey_id,
            authoritative_sender=il.sender, identity_chain=chain,
            fields=field_results, cancellation=cancel, warnings=warnings,
            query={"receipt_batch": receipt_batch,
                   "business_time": bt.iso_utc()},
            external_evidence=ext)

    # ------------------------------------------------------------- internals
    def _rebind_row(self, graph: identity.IdentityGraph, row,
                    bt: OffsetDateTime) -> identity.Binding:
        def g(k_plain: str, k_alias: str) -> str:
            return row[k_alias] if k_alias in row.keys() else row[k_plain]
        key = identity.OpKey(
            g("carrier", "k_carrier"), g("flight_no", "k_fno"),
            g("suffix", "k_sfx"), g("service_date", "k_sd"),
            g("departure", "k_dep"), g("arrival", "k_arr"), "OP")
        return graph.bind_operating(
            key, parse_offset_datetime(row["identity_at"]))

    def _root_events(self, conn, graph: identity.IdentityGraph, root_id: str,
                     batch_id: int, bt: OffsetDateTime) -> list[dict]:
        rows = conn.execute(
            "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
            "e.effective_at, e.identity_at, e.raw_ref, e.conflict_with, "
            "k.carrier AS k_carrier, k.flight_no AS k_fno, k.suffix AS k_sfx, "
            "k.service_date AS k_sd, k.departure AS k_dep, k.arrival AS k_arr "
            "FROM events e JOIN event_keys k USING(event_id,batch_id) "
            "WHERE e.batch_id<=? AND e.effective_at<=? "
            "ORDER BY e.effective_at, e.source_revision, e.event_id, e.batch_id",
            (batch_id, bt.iso_utc())).fetchall()
        out: list[dict] = []
        for r in rows:
            b = self._rebind_row(graph, dict(r), bt)
            if b.status != "bound" or b.root_id != root_id:
                continue
            d = dict(r)
            d["binding_status"] = b.status
            d["root_id"] = root_id
            d["is_authoritative"] = int(b.sender == r["sender"])
            d["binding_chain"] = b.chain
            out.append(d)
        return out

    def _resolve_fields(self, conn, events: list[dict],
                        batch_id: int) -> dict[str, FieldResult]:
        # gather authoritative updates only; external evidence is retained
        # separately and never projects fact values
        auth = [e for e in events if e["is_authoritative"] == 1]
        if not auth:
            return {f: FieldResult(f, FIELD_LABELS[f], "absent")
                    for f in FACT_FIELDS}
        placeholders = ",".join("?" for _ in auth)
        params: list = []
        for e in auth:
            params.extend([e["event_id"], e["batch_id"]])
        rows = conn.execute(
            "SELECT * FROM event_fields WHERE "
            + " OR ".join("(event_id=? AND batch_id=?)" for _ in auth),
            params).fetchall()

        # group per field
        per_field: dict[str, list[dict]] = {f: [] for f in FACT_FIELDS}
        event_index = {(e["event_id"], e["batch_id"]): e for e in auth}
        for r in rows:
            e = event_index[(r["event_id"], r["batch_id"])]
            per_field[r["field"]].append({
                "state": r["state"], "value": r["value"],
                "xml_location": r["xml_location"], "line": r["line"],
                "column": r["columnn"],
                "event_id": r["event_id"], "receipt_batch": r["batch_id"],
                "sender": e["sender"], "source_revision": e["source_revision"],
                "effective_at": e["effective_at"],
                "raw_file": e["raw_ref"],
                "priority": (e["effective_at"], e["source_revision"]),
            })

        result: dict[str, FieldResult] = {}
        for f in FACT_FIELDS:
            updates = per_field[f]
            if not updates:
                result[f] = FieldResult(f, FIELD_LABELS[f], "absent")
                continue
            max_eff = max(u["effective_at"] for u in updates)
            top_eff = [u for u in updates if u["effective_at"] == max_eff]
            max_rev = max(u["source_revision"] for u in top_eff)
            top = [u for u in top_eff if u["source_revision"] == max_rev]
            # distinct payloads at identical priority => conflict
            distinct = {(u["state"], u["value"]) for u in top}
            if len(distinct) > 1:
                result[f] = FieldResult(
                    f, FIELD_LABELS[f], "conflict", contenders=top,
                    winning=None)
                continue
            win = top[0]
            result[f] = FieldResult(
                f, FIELD_LABELS[f],
                "cleared" if win["state"] == "cleared" else "value",
                value=None if win["state"] == "cleared" else win["value"],
                contenders=updates, winning=win)
        return result

    def _resolve_cancellation(self, conn, graph, events: list[dict],
                              root_id: str, batch_id: int,
                              bt: OffsetDateTime) -> CancellationResult:
        # All cancellation messages in the knowledge cut, rebound at query
        # time so ingest-time labels are never trusted.
        rows = conn.execute(
            "SELECT c.scope, c.action, c.journey_id, c.xml_location, c.line, "
            "c.columnn AS \"column\", e.sender, e.source_revision, e.effective_at, "
            "e.identity_at, e.raw_ref, e.event_id, e.batch_id, "
            "k.carrier AS k_carrier, k.flight_no AS k_fno, k.suffix AS k_sfx, "
            "k.service_date AS k_sd, k.departure AS k_dep, k.arrival AS k_arr "
            "FROM event_cancellations c JOIN events e USING(event_id,batch_id) "
            "JOIN event_keys k USING(event_id,batch_id) "
            "WHERE e.batch_id<=? AND e.effective_at<=?",
            (batch_id, bt.iso_utc())).fetchall()
        actions: list[dict] = []
        for r in rows:
            d = dict(r)
            b = self._rebind_row(graph, d, bt)
            if b.status != "bound" or b.sender != r["sender"]:
                # only the leg's authoritative sender can cancel it
                continue
            if r["scope"] == "leg":
                if b.root_id == root_id:
                    actions.append(d)
            else:
                # journey scope: expand via identity relations known in THIS
                # batch, at the cancellation's own effective_at — never by
                # scanning 'current same flight number'.
                members = graph.legs_of_journey(
                    r["journey_id"], parse_offset_datetime(r["effective_at"]))
                if root_id in members:
                    d["journey_member_roots"] = members
                    actions.append(d)

        if not actions:
            return CancellationResult("absent")
        max_eff = max(a["effective_at"] for a in actions)
        top = [a for a in actions if a["effective_at"] == max_eff]
        max_rev = max(a["source_revision"] for a in top)
        tied = [a for a in top if a["source_revision"] == max_rev]
        distinct = {a["action"] for a in tied}
        if len(distinct) > 1:
            return CancellationResult("conflict", actions=tied)
        # restore is an explicit business operation; never infer it from a
        # field reappearing
        return CancellationResult(
            "cancelled" if tied[0]["action"] == "cancel" else "restored",
            actions=tied)

    def _external(self, conn, graph, root_id: Optional[str], batch_id: int,
                  bt: OffsetDateTime) -> list[dict]:
        if not root_id:
            return []
        rows = conn.execute(
            "SELECT x.event_id, x.sender, x.reason, x.raw_ref, x.batch_id "
            "FROM external_evidence x "
            "WHERE x.batch_id<=? ORDER BY x.batch_id, x.event_id",
            (batch_id,)).fetchall()
        out = []
        for r in rows:
            # only retain evidence whose event dynamically binds this root
            krow = conn.execute(
                "SELECT k.*, e.identity_at FROM event_keys k "
                "JOIN events e USING(event_id,batch_id) "
                "WHERE k.event_id=? AND k.batch_id=?",
                (r["event_id"], r["batch_id"])).fetchone()
            if krow is None:
                continue
            b = self._rebind_row(graph, dict(krow), bt)
            if b.status == "bound" and b.root_id == root_id:
                out.append(dict(r))
        return out

    # ------------------------------------------------------ serialization
    @staticmethod
    def to_dict(res: LegFactResult) -> dict:
        return {
            "query": res.query,
            "queried_key": res.queried_key,
            "queried_key_kind": "marketing" if res.key_kind == "MK" else "operating",
            "binding": {
                "status": res.binding_status,
                "root_id": res.root_id,
                "candidate_roots": res.candidate_roots,
                "journey_id": res.journey_id,
                "authoritative_sender": res.authoritative_sender,
                "identity_chain": res.identity_chain,
            },
            "fields": {
                f: {
                    "label": fr.label, "state": fr.state, "value": fr.value,
                    "winning_source": None if fr.winning is None else {
                        "event_id": fr.winning["event_id"],
                        "receipt_batch": fr.winning["receipt_batch"],
                        "sender": fr.winning["sender"],
                        "source_revision": fr.winning["source_revision"],
                        "effective_at": fr.winning["effective_at"],
                        "raw_file": fr.winning["raw_file"],
                        "xml_location": fr.winning["xml_location"],
                        "line": fr.winning["line"], "column": fr.winning["column"],
                    },
                    "conflict_contenders": fr.contenders if fr.state == "conflict" else [],
                }
                for f, fr in res.fields.items()
            },
            "cancellation": {"state": res.cancellation.state,
                             "actions": res.cancellation.actions},
            "warnings": res.warnings,
            "external_evidence": res.external_evidence,
        }
