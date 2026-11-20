"""Synthetic AIDX profile mapping.

Parses an AIDX envelope XML (via the position-aware expat parser) into a
normalized :class:`ParsedMessage`.  Nothing here uses string replacement on
XML: every mapped value carries an :class:`~legfact.locations.XmlNode`
location.  Unsupported elements abort fact projection and are reported.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from . import locations
from .locations import XmlNode, XmlParseError
from .timeutil import OffsetDateTime, TimeParseError, parse_offset_datetime, parse_service_date

# Canonical fact field names. Scheduled / estimated / actual never share a
# column, so a time of one category can never overwrite another.
FACT_FIELDS = (
    "sched_dep", "sched_arr",
    "est_dep", "est_arr",
    "act_dep", "act_arr",
    "dep_gate", "arr_gate",
)

PROFILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "profile", "synthetic_aidx_profile.json",
)


def load_profile(path: str | None = None) -> dict:
    with open(path or os.path.normpath(PROFILE_PATH), encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class FieldValue:
    field: str
    value: str | None          # None when cleared
    state: str                 # "value" | "cleared"
    location: str
    line: int
    column: int


@dataclass
class OperatingKeyView:
    """Operating key exactly as written in the message (an alias at identity_at)."""
    carrier: str
    flight_no: str
    suffix: str
    service_date: str
    departure: str
    arrival: str
    location: str
    loc_parts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "carrier": self.carrier, "flight_no": self.flight_no,
            "suffix": self.suffix, "service_date": self.service_date,
            "departure": self.departure, "arrival": self.arrival,
            "kind": "OP",
        }


@dataclass
class CancellationView:
    scope: str                 # "leg" | "journey"
    action: str                # "cancel" | "restore"
    journey_id: Optional[str]
    location: str
    line: int
    column: int


@dataclass
class ParsedMessage:
    event_id: str
    sender: str
    source_revision: int
    effective_at: OffsetDateTime
    identity_at: OffsetDateTime
    operating_key: OperatingKeyView
    message_type: str
    fields: list[FieldValue] = field(default_factory=list)
    cancellation: Optional[CancellationView] = None
    unsupported: list[locations.ParseIssue] = field(default_factory=list)
    envelope_locations: dict[str, str] = field(default_factory=dict)
    parse_error: Optional[str] = None
    raw_ref: str = ""

    @property
    def projectable(self) -> bool:
        return self.parse_error is None and not self.unsupported


class AidxMappingError(Exception):
    def __init__(self, message: str, node: XmlNode | None = None):
        super().__init__(message)
        self.node = node


# Map directional element names -> canonical fact field.
_TIME_ELEMENTS = {
    "ScheduledTimeOffBlocks": ("scheduled", "sched_dep"),
    "ScheduledTimeOnBlocks": ("scheduled", "sched_arr"),
    "EstimatedTimeOffBlocks": ("estimated", "est_dep"),
    "EstimatedTimeOnBlocks": ("estimated", "est_arr"),
    "ActualTimeOffBlocks": ("actual", "act_dep"),
    "ActualTimeOnBlocks": ("actual", "act_arr"),
}
_GATE_PARENTS = {"Departure": "dep_gate", "Arrival": "arr_gate"}
_TRUE = {"true", "1"}


def _text(node: XmlNode) -> str:
    return node.text.strip()


def _is_nil(node: XmlNode) -> bool:
    return node.attrs.get("nil", "").strip().lower() in _TRUE


def _require_child(parent: XmlNode, tag: str) -> XmlNode:
    n = parent.child(tag)
    if n is None:
        raise AidxMappingError(f"required element <{tag}> missing under <{parent.tag}>", parent)
    return n


def _parse_time_field(node: XmlNode, fact_field: str) -> FieldValue:
    if _is_nil(node):
        return FieldValue(fact_field, None, "cleared", node.location(), node.line, node.column)
    raw = _text(node)
    if not raw:
        # present but empty and no nil marker: treat as explicit clear per
        # profile clear_marker semantics for time elements
        return FieldValue(fact_field, None, "cleared", node.location(), node.line, node.column)
    try:
        odt = parse_offset_datetime(raw)
    except TimeParseError as exc:
        raise AidxMappingError(str(exc), node) from exc
    return FieldValue(fact_field, odt.original, "value", node.location(), node.line, node.column)


def _parse_gate(parent: XmlNode, fact_field: str) -> FieldValue:
    gate = _require_child(parent, "Gate")
    code = gate.attrs.get("code", "")
    if _is_nil(gate) or code.strip() == "" or code.strip().upper() == "NONE":
        return FieldValue(fact_field, None, "cleared", gate.location(), gate.line, gate.column)
    return FieldValue(fact_field, code.strip(), "value", gate.location(), gate.line, gate.column)


def _parse_operating_key(leg: XmlNode) -> OperatingKeyView:
    parts: dict[str, str] = {}
    loc_parts: dict[str, str] = {}
    for tag in ("OperatingCarrier", "FlightNumber", "ServiceDate",
                "DepartureStation", "ArrivalStation"):
        n = _require_child(leg, tag)
        parts[tag] = _text(n)
        loc_parts[tag] = n.location()
    suffix_node = leg.child("Suffix")
    if suffix_node is None or (_is_nil(suffix_node) or _text(suffix_node) == ""):
        suffix = ""
        if suffix_node is not None:
            loc_parts["Suffix"] = suffix_node.location()
    else:
        suffix = _text(suffix_node)
        loc_parts["Suffix"] = suffix_node.location()
    try:
        service_date = parse_service_date(parts["ServiceDate"])
    except TimeParseError as exc:
        raise AidxMappingError(str(exc), leg.child("ServiceDate")) from exc
    flight_no = parts["FlightNumber"]
    if not flight_no.isdigit():
        raise AidxMappingError(f"FlightNumber must be numeric, got {flight_no!r}",
                               leg.child("FlightNumber"))
    return OperatingKeyView(
        carrier=parts["OperatingCarrier"], flight_no=flight_no, suffix=suffix,
        service_date=service_date, departure=parts["DepartureStation"],
        arrival=parts["ArrivalStation"], location=leg.location(), loc_parts=loc_parts,
    )


def _collect_unsupported(root: XmlNode, profile: dict) -> list[locations.ParseIssue]:
    bad = set(profile["unsupported_elements"]["elements"])
    bad_attrs = {"schemaLocation"}
    out: list[locations.ParseIssue] = []

    def walk(n: XmlNode) -> None:
        if n.tag in bad:
            out.append(locations.ParseIssue(
                "unsupported_element", n.tag, n.line, n.column, n.location(),
                "element is outside the synthetic AIDX profile "
                "(scheduling/booking/optimization domain)",
            ))
        for a in n.attrs:
            if a in bad_attrs:
                ln, col = n.attr_loc[a]
                out.append(locations.ParseIssue(
                    "unsupported_attribute", a, ln, col, n.location(),
                    "attribute is not interpreted by this profile",
                ))
        for c in n.children:
            walk(c)

    walk(root)
    return out


def parse_aidx(data: bytes | str, profile: dict | None = None,
               raw_ref: str = "") -> ParsedMessage:
    profile = profile or load_profile()
    env_loc: dict[str, str] = {}
    try:
        root, ns_issues = locations.parse_xml(
            data, {profile["default_namespace"]})
    except XmlParseError as exc:
        # Malformed XML: keep a tombstone record, not projected.
        pm = ParsedMessage.__new__(ParsedMessage)
        pm.event_id = ""
        pm.sender = ""
        pm.source_revision = 0
        pm.effective_at = None  # type: ignore[assignment]
        pm.identity_at = None  # type: ignore[assignment]
        pm.operating_key = None  # type: ignore[assignment]
        pm.message_type = "MalformedXml"
        pm.fields = []
        pm.cancellation = None
        pm.unsupported = []
        pm.envelope_locations = {}
        pm.parse_error = f"XML parse error at line {exc.line}:{exc.column}: {exc}"
        pm.raw_ref = raw_ref
        return pm

    if root.tag != "AIDXMessageEnvelope":
        raise AidxMappingError(
            f"root element must be AIDXMessageEnvelope, got <{root.tag}>", root)

    unsupported = list(ns_issues) + _collect_unsupported(root, profile)

    def env_text(tag: str) -> tuple[str, str]:
        n = _require_child(root, tag)
        return _text(n), n.location()

    try:
        event_id, loc = env_text("EventID")
        env_loc["EventID"] = loc
        sender, loc = env_text("Sender")
        env_loc["Sender"] = loc
        rev_text, loc = env_text("SourceRevision")
        env_loc["SourceRevision"] = loc
        if not rev_text.isdigit() or int(rev_text) <= 0:
            raise AidxMappingError(f"SourceRevision must be a positive integer, got {rev_text!r}",
                                   root.child("SourceRevision"))
        source_revision = int(rev_text)
        eff_text, loc = env_text("EffectiveAt")
        env_loc["EffectiveAt"] = loc
        effective_at = parse_offset_datetime(eff_text)
        ident_text, loc = env_text("IdentityAt")
        env_loc["IdentityAt"] = loc
        identity_at = parse_offset_datetime(ident_text)
        mtype_node = _require_child(root, "MessageType")
        message_type = _text(mtype_node)
        env_loc["MessageType"] = mtype_node.location()
        if message_type not in ("FlightUpdate", "Cancellation"):
            raise AidxMappingError(f"unsupported MessageType {message_type!r}", mtype_node)
        leg = _require_child(root, "FlightLeg")
        env_loc["FlightLeg"] = leg.location()
        operating_key = _parse_operating_key(leg)
    except (AidxMappingError, TimeParseError) as exc:
        pm = ParsedMessage.__new__(ParsedMessage)
        pm.event_id = locals().get("event_id", "")
        pm.sender = locals().get("sender", "")
        pm.source_revision = int(locals().get("source_revision", 0) or 0)
        pm.effective_at = locals().get("effective_at")
        pm.identity_at = locals().get("identity_at")
        pm.operating_key = locals().get("operating_key")
        pm.message_type = "MappingError"
        pm.fields = []
        pm.cancellation = None
        pm.unsupported = unsupported
        pm.envelope_locations = env_loc
        pm.parse_error = str(exc)
        pm.raw_ref = raw_ref
        return pm

    fields: list[FieldValue] = []
    cancellation: Optional[CancellationView] = None

    if message_type == "FlightUpdate":
        try:
            for node in leg.children:
                if node.tag in _TIME_ELEMENTS:
                    _category, fact = _TIME_ELEMENTS[node.tag]
                    fields.append(_parse_time_field(node, fact))
                elif node.tag in _GATE_PARENTS:
                    fields.append(_parse_gate(node, _GATE_PARENTS[node.tag]))
            # Gate may also be directly on the leg for departure (profile allows
            # both forms); avoid double counting.
            direct_gate = leg.child("Gate")
            if direct_gate is not None and not any(f.field == "dep_gate" for f in fields):
                code = direct_gate.attrs.get("code", "")
                if _is_nil(direct_gate) or code.strip() in ("", "NONE"):
                    fv = FieldValue("dep_gate", None, "cleared",
                                    direct_gate.location(), direct_gate.line,
                                    direct_gate.column)
                else:
                    fv = FieldValue("dep_gate", code.strip(), "value",
                                    direct_gate.location(), direct_gate.line,
                                    direct_gate.column)
                fields.append(fv)
        except (AidxMappingError, TimeParseError) as exc:
            return _mapping_error(
                str(exc), getattr(exc, "node", None), event_id, sender,
                source_revision, effective_at, identity_at, operating_key,
                unsupported, env_loc, raw_ref)

    cancel_node = root.child("Cancellation")
    if cancel_node is not None:
        scope = cancel_node.attrs.get("scope", "").strip()
        action = cancel_node.attrs.get("action", "").strip()
        jid = cancel_node.attrs.get("journey_id") or None
        if scope not in ("leg", "journey") or action not in ("cancel", "restore"):
            return _mapping_error(
                f"Cancellation requires scope in [leg,journey] and action in [cancel,restore], "
                f"got scope={scope!r} action={action!r}",
                cancel_node, event_id, sender, source_revision, effective_at,
                identity_at, operating_key, unsupported, env_loc, raw_ref)
        if scope == "journey" and not jid:
            return _mapping_error(
                "journey-scoped Cancellation requires journey_id attribute",
                cancel_node, event_id, sender, source_revision, effective_at,
                identity_at, operating_key, unsupported, env_loc, raw_ref)
        cancellation = CancellationView(scope, action, jid,
                                        cancel_node.location(),
                                        cancel_node.line, cancel_node.column)
        if message_type != "Cancellation":
            # profile permits Cancellation section inside either envelope type
            message_type = "Cancellation"

    return ParsedMessage(
        event_id=event_id, sender=sender, source_revision=source_revision,
        effective_at=effective_at, identity_at=identity_at,
        operating_key=operating_key, message_type=message_type, fields=fields,
        cancellation=cancellation, unsupported=unsupported,
        envelope_locations=env_loc, parse_error=None, raw_ref=raw_ref,
    )


def _mapping_error(msg, node, event_id, sender, rev, eff, ident, key,
                   unsupported, env_loc, raw_ref) -> ParsedMessage:
    pm = ParsedMessage.__new__(ParsedMessage)
    pm.event_id = event_id or ""
    pm.sender = sender or ""
    pm.source_revision = rev or 0
    pm.effective_at = eff
    pm.identity_at = ident
    pm.operating_key = key
    pm.message_type = "MappingError"
    pm.fields = []
    pm.cancellation = None
    pm.unsupported = unsupported
    pm.envelope_locations = env_loc
    pm.parse_error = f"{msg} ({node.location() if node else ''})"
    pm.raw_ref = raw_ref
    return pm
