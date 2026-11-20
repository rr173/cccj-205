"""Identity relations: initial legs, renames and codeshare aliases.

The identity file is an *explicit input* delivered in a batch.  Nothing here
merges legs by flight number, codeshare number or arrival order:

* ``initial_leg`` declares a root operating key, its ``valid_from`` and the
  authoritative sender;
* ``rename`` retires an old alias at ``effective_at`` and introduces a new
  alias for the *same continuous operating leg*; it can never fuse two
  separately declared roots;
* ``codeshare`` maps a marketing key onto an operating key over a half-open
  interval and is query-only — it never merges operating legs.

Ambiguities (overlapping alias intervals, rename forks/cycles, unknown roots,
double-meaning mappings) are retained as explicit ambiguity records.  Forward
references are resolvable once the batch carrying the referenced root closes,
so they are not guessed at ingest time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .timeutil import OffsetDateTime, TimeParseError, parse_offset_datetime, parse_service_date


@dataclass(frozen=True)
class OpKey:
    """An operating (or marketing) key tuple.

    ``kind`` distinguishes operating keys (``"OP"``) from marketing keys
    (``"MK"``) so a shared numeric flight number can never collide silently.
    """

    carrier: str
    flight_no: str
    suffix: str
    service_date: str
    departure: str
    arrival: str
    kind: str = "OP"

    def as_tuple(self) -> tuple:
        return (self.carrier, self.flight_no, self.suffix, self.service_date,
                self.departure, self.arrival, self.kind)

    def to_dict(self) -> dict:
        return {
            "carrier": self.carrier, "flight_no": self.flight_no,
            "suffix": self.suffix, "service_date": self.service_date,
            "departure": self.departure, "arrival": self.arrival,
            "kind": self.kind,
        }

    def pretty(self) -> str:
        sfx = f"/{self.suffix}" if self.suffix else ""
        return (f"{self.carrier}{self.flight_no}{sfx} "
                f"{self.service_date} {self.departure}->{self.arrival}")


@dataclass
class InitialLeg:
    root_id: str
    key: OpKey
    valid_from: OffsetDateTime
    sender: str
    journey_id: str
    source: dict          # raw relation record for provenance


@dataclass
class Rename:
    rid: str
    old_key: OpKey
    new_key: OpKey
    effective_at: OffsetDateTime
    source: dict


@dataclass
class Codeshare:
    cid: str
    marketing_key: OpKey
    operating_key: OpKey
    valid_from: OffsetDateTime
    valid_to: Optional[OffsetDateTime]   # half-open; None => open ended
    source: dict


@dataclass
class IdentityWarning:
    code: str             # see AMBIGUITY_CODES
    detail: str
    relations: list[str] = field(default_factory=list)


AMBIGUITY_CODES = (
    "rename_unknown_root",      # rename chain reaches a key no initial_leg owns
    "rename_fork",              # one alias renamed to two different successors
    "rename_cycle",             # rename edges form a cycle
    "rename_double_root",       # rename would fuse two declared roots
    "codeshare_unknown_root",   # codeshare target is not a known operating root
    "codeshare_overlap",        # one marketing key -> >=2 roots at one instant
    "double_meaning",           # one key used as both operating and marketing alias
)


class IdentityFileError(ValueError):
    pass


def _key(d: dict, kind: str) -> OpKey:
    required = ("carrier", "flight_no", "suffix", "service_date",
                "departure", "arrival")
    missing = [k for k in required if k not in d]
    if missing:
        raise IdentityFileError(f"key record missing fields {missing}: {d}")
    if not str(d["flight_no"]).isdigit():
        raise IdentityFileError(f"flight_no must be numeric: {d}")
    try:
        sd = parse_service_date(d["service_date"])
    except TimeParseError as exc:
        raise IdentityFileError(str(exc)) from exc
    return OpKey(
        carrier=str(d["carrier"]), flight_no=str(d["flight_no"]),
        suffix=str(d.get("suffix") or ""), service_date=sd,
        departure=str(d["departure"]), arrival=str(d["arrival"]), kind=kind,
    )


def parse_identity_file(data: bytes | str | dict) -> tuple[list[InitialLeg],
                                                           list[Rename],
                                                           list[Codeshare]]:
    if isinstance(data, (bytes, str)):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        try:
            doc = json.loads(data)
        except json.JSONDecodeError as exc:
            raise IdentityFileError(f"identity file is not valid JSON: {exc}") from exc
    else:
        doc = data
    if not isinstance(doc, dict) or not isinstance(doc.get("relations"), list):
        raise IdentityFileError("identity file must be an object with a 'relations' array")

    initials: list[InitialLeg] = []
    renames: list[Rename] = []
    codeshares: list[Codeshare] = []
    seen_rid: set[str] = set()

    for i, rec in enumerate(doc["relations"]):
        if not isinstance(rec, dict) or "type" not in rec:
            raise IdentityFileError(f"relation #{i} must be an object with a 'type'")
        rid = rec.get("id") or f"rel#{i}"
        if rid in seen_rid:
            raise IdentityFileError(f"duplicate relation id {rid!r}")
        seen_rid.add(rid)
        rtype = rec["type"]
        try:
            if rtype == "initial_leg":
                key = _key(rec["initial_key"], "OP")
                initials.append(InitialLeg(
                    root_id=rec["root_id"], key=key,
                    valid_from=parse_offset_datetime(rec["valid_from"]),
                    sender=rec["sender"], journey_id=rec["journey_id"],
                    source=rec,
                ))
            elif rtype == "rename":
                renames.append(Rename(
                    rid=rid, old_key=_key(rec["old_key"], "OP"),
                    new_key=_key(rec["new_key"], "OP"),
                    effective_at=parse_offset_datetime(rec["effective_at"]),
                    source=rec,
                ))
            elif rtype == "codeshare":
                vt = rec.get("valid_to")
                codeshares.append(Codeshare(
                    cid=rid, marketing_key=_key(rec["marketing_key"], "MK"),
                    operating_key=_key(rec["operating_key"], "OP"),
                    valid_from=parse_offset_datetime(rec["valid_from"]),
                    valid_to=parse_offset_datetime(vt) if vt else None,
                    source=rec,
                ))
            else:
                raise IdentityFileError(f"unknown relation type {rtype!r} in {rid}")
        except KeyError as exc:
            raise IdentityFileError(f"relation {rid} missing field {exc}") from exc
        except TimeParseError as exc:
            raise IdentityFileError(f"relation {rid}: {exc}") from exc

    root_ids = [x.root_id for x in initials]
    if len(set(root_ids)) != len(root_ids):
        raise IdentityFileError("duplicate root_id in initial_leg declarations")
    key_seen: dict[tuple, str] = {}
    for x in initials:
        t = x.key.as_tuple()
        if t in key_seen:
            raise IdentityFileError(
                f"initial key {x.key.pretty()} declared by two roots: "
                f"{key_seen[t]} and {x.root_id}")
        key_seen[t] = x.root_id
    return initials, renames, codeshares


@dataclass
class Binding:
    """Result of binding a message key at an identity instant."""
    status: str                 # "bound" | "ambiguous" | "unknown" | "forward"
    root_id: Optional[str] = None
    journey_id: Optional[str] = None
    sender: Optional[str] = None
    chain: list[dict] = field(default_factory=list)   # identity association chain
    warnings: list[IdentityWarning] = field(default_factory=list)
    candidate_roots: list[str] = field(default_factory=list)


class IdentityGraph:
    """Materialized identity knowledge of one published knowledge snapshot.

    Built once per publish from the *accumulated* relations of all batches up
    to and including that batch.  Lookups are keyed by exact key tuples plus
    half-open interval tests, so alias paths are followed on demand (with a
    visited guard) rather than enumerated into memory.
    """

    def __init__(self) -> None:
        self.initials: dict[str, InitialLeg] = {}
        self.initial_key_to_root: dict[tuple, str] = {}
        # operating key -> rename records where it is old/new
        self.rename_out: dict[tuple, list[Rename]] = {}
        self.rename_in: dict[tuple, list[Rename]] = {}
        # marketing key tuple -> codeshare records
        self.codeshares: dict[tuple, list[Codeshare]] = {}
        # every operating key tuple claimed by a root (initial or rename target)
        self.operating_keys: dict[tuple, str] = {}
        self.warnings: list[IdentityWarning] = []

    # ------------------------------------------------------------------ build
    @classmethod
    def build(cls, initials: list[InitialLeg], renames: list[Rename],
              codeshares: list[Codeshare]) -> "IdentityGraph":
        g = cls()
        for il in initials:
            g.initials[il.root_id] = il
            g.initial_key_to_root[il.key.as_tuple()] = il.root_id
            g.operating_keys[il.key.as_tuple()] = il.root_id

        # ---- rename validation: forks, cycles, double-root, unknown roots
        for r in renames:
            g.rename_out.setdefault(r.old_key.as_tuple(), []).append(r)
            g.rename_in.setdefault(r.new_key.as_tuple(), []).append(r)

        for old_t, outs in g.rename_out.items():
            successors = {r.new_key.as_tuple() for r in outs}
            if len(successors) > 1:
                g.warnings.append(IdentityWarning(
                    "rename_fork",
                    f"alias {outs[0].old_key.pretty()} renamed to multiple successors",
                    [r.rid for r in outs]))

        # ---- component analysis via union-find (near-linear, no chain walks)
        parent: dict[tuple, tuple] = {}

        def find(x: tuple) -> tuple:
            parent.setdefault(x, x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:  # path compression
                parent[x], x = root, parent[x]
            return root

        def union(a: tuple, b: tuple) -> None:
            parent[find(a)] = find(b)

        # seed every initial key, then join the endpoints of each rename edge
        for t in g.initial_key_to_root:
            find(t)
        for r in renames:
            union(r.old_key.as_tuple(), r.new_key.as_tuple())

        # component -> initial roots it touches, and -> rename rids inside it
        comp_roots: dict[tuple, set[str]] = {}
        comp_rels: dict[tuple, list[str]] = {}
        for t, root in g.initial_key_to_root.items():
            comp_roots.setdefault(find(t), set()).add(root)
        for r in renames:
            c = find(r.old_key.as_tuple())
            comp_rels.setdefault(c, []).append(r.rid)

        # rename double-root: one component fuses two separately declared roots
        for c, roots in comp_roots.items():
            if len(roots) > 1:
                g.warnings.append(IdentityWarning(
                    "rename_double_root",
                    f"rename component would fuse distinct declared roots "
                    f"{sorted(roots)}", comp_rels.get(c, [])))
        # unknown-root components: reach no initial_leg at all (one warning
        # per disconnected root-less component, not per edge)
        rootless_warned: set[tuple] = set()
        for r in renames:
            c = find(r.old_key.as_tuple())
            if c not in comp_roots and c not in rootless_warned:
                rootless_warned.add(c)
                g.warnings.append(IdentityWarning(
                    "rename_unknown_root",
                    "rename component reaches no initial_leg",
                    comp_rels.get(c, [])))

        # assign rename target keys to a root only when the component has a
        # single unambiguous root; multi-root components stay unassigned.
        for r in renames:
            c = find(r.old_key.as_tuple())
            roots = comp_roots.get(c)
            if not roots or len(roots) != 1:
                continue
            root = next(iter(roots))
            t = r.new_key.as_tuple()
            g.operating_keys.setdefault(t, root)

        # cycle detection across rename edges (iterative, recursion-safe)
        g._detect_rename_cycles(renames)

        # ---- codeshare validation
        for c in codeshares:
            g.codeshares.setdefault(c.marketing_key.as_tuple(), []).append(c)
            target_root = g.operating_keys.get(c.operating_key.as_tuple())
            if target_root is None:
                # may be a forward reference resolvable in a later batch
                g.warnings.append(IdentityWarning(
                    "codeshare_unknown_root",
                    f"codeshare {c.cid} target {c.operating_key.pretty()} "
                    f"is not a known operating root in this snapshot", [c.cid]))

        for mk, recs in g.codeshares.items():
            # interval overlap -> same instant maps to >=2 roots
            recs_sorted = sorted(recs, key=lambda c: c.valid_from.utc)
            for i in range(len(recs_sorted)):
                a = recs_sorted[i]
                for j in range(i + 1, len(recs_sorted)):
                    b = recs_sorted[j]
                    a_end = a.valid_to.utc if a.valid_to else None
                    if a_end is None or a_end > b.valid_from.utc:
                        roots = {g.operating_keys.get(c.operating_key.as_tuple())
                                 for c in (a, b)}
                        roots.discard(None)
                        if len(roots) != 1:
                            g.warnings.append(IdentityWarning(
                                "codeshare_overlap",
                                f"marketing key {a.marketing_key.pretty()} has overlapping "
                                f"codeshares {a.cid},{b.cid} at {b.valid_from.iso_utc()}",
                                [a.cid, b.cid]))

        # double meaning: same tuple used as operating key and marketing key
        for mk in g.codeshares:
            if mk in g.operating_keys:
                g.warnings.append(IdentityWarning(
                    "double_meaning",
                    f"key tuple {mk} is both an operating alias and a marketing alias",
                    [c.cid for c in g.codeshares[mk]]))
        return g

    def _detect_rename_cycles(self, renames: list[Rename]) -> None:
        # Iterative three-color DFS (0 white, 1 gray, 2 black) with an explicit
        # work stack, so deep rename graphs cannot hit the recursion limit.
        color: dict[tuple, int] = {}
        for start in renames:
            s = start.old_key.as_tuple()
            if color.get(s, 0) != 0:
                continue
            work: list[tuple] = [(s, 0)]   # (node, next-edge-index)
            path: list[tuple] = []
            while work:
                t, idx = work[-1]
                if idx == 0:
                    color[t] = 1
                    path.append(t)
                outs = self.rename_out.get(t, ())
                if idx < len(outs):
                    work[-1] = (t, idx + 1)
                    nt = outs[idx].new_key.as_tuple()
                    if color.get(nt, 0) == 1 and nt in path:
                        cyc = path[path.index(nt):] + [nt]
                        self.warnings.append(IdentityWarning(
                            "rename_cycle",
                            "rename cycle: " + " -> ".join(str(x) for x in cyc),
                            []))
                    elif color.get(nt, 0) == 0:
                        work.append((nt, 0))
                else:
                    color[t] = 2
                    path.pop()
                    work.pop()

    # ----------------------------------------------------------------- query
    def bind_operating(self, key: OpKey, identity_at: OffsetDateTime) -> Binding:
        """Bind an operating key as written in a message at ``identity_at``.

        Alias intervals for rename chains are half-open: an old alias is
        current until the rename's ``effective_at`` (exclusive), then the new
        alias takes over.  Late messages may still bind through a retired
        alias because binding uses ``identity_at``, not receipt time.

        An *initial* key always binds its declared root directly; problematic
        renames attached elsewhere are surfaced as warnings rather than
        poisoning the root's own identity.
        """
        t = key.as_tuple()
        chain: list[dict] = []
        warnings: list[IdentityWarning] = []

        # Fast path: the tuple is itself a declared initial key.
        if t in self.initial_key_to_root:
            root = self.initial_key_to_root[t]
            chain.append({"key": _pretty(t), "via": "initial_leg", "root_id": root})
            attached = self._warnings_touching(t)
            if attached:
                il = self.initials[root]
                return Binding("ambiguous", root_id=root, journey_id=il.journey_id,
                               sender=il.sender, chain=chain,
                               candidate_roots=sorted({root} |
                                                      self._candidate_roots_forward(t, set())),
                               warnings=attached)
            il = self.initials[root]
            return Binding("bound", root_id=root, journey_id=il.journey_id,
                           sender=il.sender, chain=chain, warnings=warnings)

        # fork directly visible at this tuple?
        if t in self.rename_out and len({r.new_key.as_tuple()
                                         for r in self.rename_out[t]}) > 1:
            roots = self._candidate_roots_forward(t, set())
            return Binding("ambiguous", candidate_roots=sorted(roots),
                           chain=chain,
                           warnings=[w for w in self.warnings
                                     if w.code == "rename_fork"])

        # Walk the chain: current tuple at identity_at, follow rename history.
        root_id = self._bind_walk(t, identity_at, chain, set(), warnings)
        if root_id is None:
            if self._is_forward_reference(t):
                return Binding("forward", chain=chain, warnings=warnings)
            return Binding("unknown", chain=chain, warnings=warnings)

        # ambiguity attached to this root's edges still makes binding ambiguous
        blocking = [w for w in warnings
                    if w.code in ("rename_cycle", "rename_fork", "rename_double_root")]
        if blocking:
            return Binding("ambiguous", root_id=root_id,
                           candidate_roots=sorted(set([root_id] +
                                              self._candidate_roots_forward(t, set()))),
                           chain=chain, warnings=warnings)
        il = self.initials[root_id]
        return Binding("bound", root_id=root_id, journey_id=il.journey_id,
                       sender=il.sender, chain=chain, warnings=warnings)

    def _warnings_touching(self, t: tuple) -> list[IdentityWarning]:
        """Snapshot warnings whose rename EDGES actually mention tuple ``t``.

        A declared initial key is only ambiguous when a problematic rename
        edge carries that exact tuple; unrelated forks/cycles elsewhere in
        the graph must not poison it.
        """
        touched_rels: set[str] = set()
        for r in self.rename_out.get(t, ()):
            touched_rels.add(r.rid)
        for r in self.rename_in.get(t, ()):
            touched_rels.add(r.rid)
        if not touched_rels:
            return []
        return [w for w in self.warnings
                if w.code in ("rename_fork", "rename_cycle",
                              "rename_double_root")
                and any(rel in touched_rels for rel in w.relations)]

    def _bind_walk(self, t: tuple, at: OffsetDateTime, chain: list[dict],
                   seen: set, warnings: list[IdentityWarning]) -> Optional[str]:
        """Iterative alias walk following rename history back to a root.

        An explicit work stack keeps deep chains linear in memory; the
        ``seen`` guard makes cycles terminate instead of looping.
        """
        work: list[tuple] = [t]
        local_seen = set(seen)
        while work:
            cur = work.pop()
            if cur in local_seen:
                warnings.append(
                    IdentityWarning("rename_cycle", "cycle hit during binding"))
                return None
            local_seen.add(cur)
            if cur in self.initial_key_to_root:
                root = self.initial_key_to_root[cur]
                chain.append({"key": _pretty(cur), "via": "initial_leg",
                              "root_id": root})
                return root
            ins = self.rename_in.get(cur)
            if ins:
                eligible = [r for r in ins if at.utc >= r.effective_at.utc] or ins
                r = eligible[0]
                chain.append({"key": _pretty(cur), "via": "rename",
                              "relation": r.rid,
                              "effective_at": r.effective_at.iso_utc(),
                              "old_key": r.old_key.pretty()})
                work.append(r.old_key.as_tuple())
                continue
            outs = self.rename_out.get(cur)
            if outs:
                prior = [r for r in outs if at.utc < r.effective_at.utc]
                if not prior and len({r.new_key.as_tuple() for r in outs}) > 1:
                    warnings.append(
                        IdentityWarning("rename_fork", "fork at legacy alias"))
                    return None
                chosen = prior[0] if prior else outs[0]
                chain.append({"key": _pretty(cur),
                              "via": "rename(legacy alias)",
                              "relation": chosen.rid,
                              "effective_at": chosen.effective_at.iso_utc(),
                              "new_key": chosen.new_key.pretty()})
                work.append(chosen.new_key.as_tuple())
                continue
            return None
        return None

    def _candidate_roots_forward(self, t: tuple, seen: set) -> set[str]:
        """Iterative undirected reachability over rename edges (bounded)."""
        out: set[str] = set()
        visited = set(seen)
        stack = [t]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            if cur in self.initial_key_to_root:
                out.add(self.initial_key_to_root[cur])
            for r in self.rename_out.get(cur, ()):
                stack.append(r.new_key.as_tuple())
            for r in self.rename_in.get(cur, ()):
                stack.append(r.old_key.as_tuple())
        return out

    def _is_forward_reference(self, t: tuple) -> bool:
        """True if the tuple appears in relation edges but no root owns it yet.

        A key carried by an incoming operating message is a forward reference
        when it is mentioned by rename edges, or is the operating target of a
        codeshare, while no initial_leg has declared it in this snapshot.
        """
        if self.rename_out.get(t) or self.rename_in.get(t):
            return True
        for recs in self.codeshares.values():
            for c in recs:
                if c.operating_key.as_tuple() == t:
                    return True
        return False

    def bind_marketing(self, key: OpKey, identity_at: OffsetDateTime) -> Binding:
        t = key.as_tuple()
        recs = self.codeshares.get(t, [])
        active = [c for c in recs
                  if c.valid_from.utc <= identity_at.utc
                  and (c.valid_to is None or identity_at.utc < c.valid_to.utc)]
        if not active:
            if recs:
                return Binding("forward", chain=[{"key": key.pretty(),
                                                  "via": "codeshare(outside interval)"}])
            return Binding("unknown", chain=[{"key": key.pretty(), "via": "codeshare"}])
        roots = set()
        chain = [{"key": key.pretty(), "via": "codeshare",
                  "relations": [c.cid for c in active]}]
        for c in active:
            root = self.operating_keys.get(c.operating_key.as_tuple())
            if root:
                roots.add(root)
                chain.append({"marketing": c.marketing_key.pretty(),
                              "operating": c.operating_key.pretty(),
                              "relation": c.cid, "root_id": root,
                              "valid_from": c.valid_from.iso_utc(),
                              "valid_to": c.valid_to.iso_utc() if c.valid_to else None})
        if len(roots) > 1:
            return Binding("ambiguous", candidate_roots=sorted(roots), chain=chain,
                           warnings=[w for w in self.warnings
                                     if w.code == "codeshare_overlap"])
        if not roots:
            return Binding("forward", chain=chain)
        root = next(iter(roots))
        il = self.initials[root]
        return Binding("bound", root_id=root, journey_id=il.journey_id,
                       sender=il.sender, chain=chain)

    def legs_of_journey(self, journey_id: str, at: OffsetDateTime) -> list[str]:
        """Roots that belong to ``journey_id`` and already exist at ``at``.

        A root exists from its initial ``valid_from``; journey ownership is
        fixed for the whole exercise.  We deliberately do not scan 'current
        same flight number' legs.
        """
        out = []
        for root_id, il in self.initials.items():
            if il.journey_id == journey_id and il.valid_from.utc <= at.utc:
                out.append(root_id)
        return sorted(out)

    def warning_index(self) -> dict[str, list[IdentityWarning]]:
        idx: dict[str, list[IdentityWarning]] = {}
        for w in self.warnings:
            idx.setdefault(w.code, []).append(w)
        return idx


def _pretty(t: tuple) -> str:
    carrier, fno, suffix, sd, dep, arr, kind = t
    sfx = f"/{suffix}" if suffix else ""
    return f"[{kind}]{carrier}{fno}{sfx} {sd} {dep}->{arr}"
