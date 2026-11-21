"""Single-pass authoritative actual-time lookup for reconciliation roots.

The visit roster names up to two operating roots per turnaround (the inbound
leg and the next outbound leg).  Resolving each root with the full query engine
would re-scan every flight message once per root; instead we make ONE pass over
the knowledge cut's ``act_arr`` / ``act_dep`` field rows — exactly the trick the
landing-fee mart uses — and adjudicate each root with the warehouse's own
canonical field rule (:func:`legfact.query.resolve_field_updates`).

The pass is bounded by the published ``receipt_batch`` and the fact cutoff:
only messages ``effective_at <= cutoff`` participate, and an actual value
itself later than the cutoff is reported as ``actual_after_cutoff`` rather than
used.
"""
from __future__ import annotations

from datetime import datetime

from . import identity
from .query import FieldResult, resolve_field_updates
from .storage import Warehouse
from .timeutil import parse_offset_datetime

# Unavailability reasons for roster rows (mirrored in the docs and issue table).
R_UNKNOWN_ROOT = "unknown_root"
R_AIRPORT_MISMATCH = "airport_mismatch"
R_ACTUAL_UNCERTAIN = "actual_uncertain"
R_DEP_NOT_AFTER_ARR = "departure_not_after_arrival"
R_AFTER_CUTOFF = "actual_after_cutoff"


def _iso_ceiling(cutoff: datetime) -> str:
    from .landing import _ceil_second, _iso_z
    return _iso_z(_ceil_second(cutoff))


def scan_actuals(wh: Warehouse, batch_id: int, cutoff: datetime,
                 roots: set[str], fields: tuple[str, ...] = ("act_arr", "act_dep")
                 ) -> dict[str, dict[str, list]]:
    """One SQL pass per fact field, returning winning buckets keyed by root.

    A bucket is ``[effective_at, source_revision, {(state,value): evidence}]``
    — the exact input shape :func:`resolve_field_updates` understands.  Roots
    outside ``roots`` are discarded while streaming, so memory stays bounded by
    the roster, not by the warehouse.
    """
    graph = wh.load_graph(batch_id)
    conn = wh._conn
    ceiling = _iso_ceiling(cutoff)
    winners: dict[str, dict[str, list]] = {r: {} for r in roots}
    bind_cache: dict[bytes, str | None] = {}

    for field_name in fields:
        cur = conn.execute(
            "SELECT e.event_id, e.batch_id, e.sender, e.source_revision, "
            "e.effective_at, e.identity_at, e.raw_ref, "
            "k.carrier, k.flight_no, k.suffix, k.service_date, "
            "k.departure, k.arrival, "
            "f.state, f.value, f.xml_location, f.line, f.columnn "
            "FROM event_fields f "
            "JOIN events e ON e.event_id=f.event_id AND e.batch_id=f.batch_id "
            "JOIN event_keys k ON k.event_id=e.event_id AND k.batch_id=e.batch_id "
            "WHERE f.field=? AND e.batch_id<=? AND e.effective_at<=? "
            "ORDER BY e.effective_at, e.source_revision, e.event_id, e.batch_id",
            (field_name, batch_id, ceiling))
        for r in cur:
            if parse_offset_datetime(r["effective_at"]).utc > cutoff:
                continue
            sender = r["sender"]
            bind_key = (r["carrier"] + "\x1f" + r["flight_no"] + "\x1f"
                        + r["suffix"] + "\x1f" + r["service_date"] + "\x1f"
                        + r["departure"] + "\x1f" + r["arrival"] + "\x1f"
                        + r["identity_at"] + "\x1f" + sender).encode()
            root = bind_cache.get(bind_key, _MISS)
            if root is _MISS:
                key = identity.OpKey(
                    r["carrier"], r["flight_no"], r["suffix"],
                    r["service_date"], r["departure"], r["arrival"], "OP")
                b = graph.bind_operating(
                    key, parse_offset_datetime(r["identity_at"]))
                root = (b.root_id if b.status == "bound"
                        and b.sender == sender else None)
                bind_cache[bind_key] = root
            if root is None or root not in winners:
                continue
            rev = r["source_revision"]
            eff = r["effective_at"]
            payload = (r["state"], r["value"])
            ev = (r["value"], r["event_id"], r["batch_id"], r["xml_location"],
                  r["line"], r["columnn"])
            bucket = winners[root].get(field_name)
            if bucket is None:
                winners[root][field_name] = [eff, rev, {payload: ev}]
            elif eff > bucket[0] or (eff == bucket[0] and rev > bucket[1]):
                bucket[0], bucket[1], bucket[2] = eff, rev, {payload: ev}
            elif eff == bucket[0] and rev == bucket[1]:
                bucket[2].setdefault(payload, ev)
    return winners


_MISS = object()


def adjudicate(bucket: list | None) -> FieldResult:
    """Run the warehouse field rule over one root's winning bucket."""
    if not bucket:
        return FieldResult("x", "x", "absent")
    eff, rev, top = bucket
    vals = [{"state": st, "value": va, "effective_at": eff,
             "source_revision": rev, "evidence": ev}
            for (st, va), ev in top.items()]
    return resolve_field_updates("x", vals)


def evidence_of(fr: FieldResult) -> list[dict]:
    """Serialize the fact evidence in the same shape as the landing mart."""
    if fr.state == "conflict":
        out = []
        for c in fr.contenders or []:
            ev = c["evidence"]
            out.append(_ev_dict(ev))
        return out
    if fr.winning is not None:
        return [_ev_dict(fr.winning["evidence"])]
    return []


def _ev_dict(ev: tuple) -> dict:
    value, event_id, batch_id, xml_loc, line, col = ev
    return {"event_id": event_id, "receipt_batch": batch_id,
            "raw_value": value, "xml_location": xml_loc,
            "line": line, "column": col}
