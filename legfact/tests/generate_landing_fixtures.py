#!/usr/bin/env python3
"""Deterministic landing-fee mart fixtures.

Reuses the warehouse's existing synthetic AIDX builders (same XML profile,
same identity relation format) — no second flight-XML dialect.  Writes:

  fixtures/landing/walkthrough/   hand-computable A/B/C handover scenario
  fixtures/landing/edge/          ordering, time-zone/rate boundaries, missing
                                  actual vs zero amount, duplicates, weights
  fixtures/landing/invalid/       structurally invalid dimension files
  fixtures/landing/expected/      hand-written expectation tables (not produced
                                  by the mart implementation)

Run:  python3 -I tests/generate_landing_fixtures.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generate_fixtures import (  # noqa: E402
    aidx_xml, identity_file, rel_initial, rel_rename, write_batch)

AUTH = "SHA-OCC"
BASE = os.path.abspath(os.path.join(HERE, "..", "fixtures", "landing"))
T0 = "2026-01-01T00:00:00+08:00"


def key(fno: str, service_date: str = "2026-01-01",
        dep: str = "PEK", arr: str = "SHA") -> dict:
    return {"carrier": "CA", "flight_no": fno, "suffix": "",
            "service_date": service_date, "departure": dep, "arrival": arr}


def msg(event_id: str, k: dict, actual: str, *, revision: int = 1,
        effective_at: str, identity_at: str = T0,
        extra_times: dict | None = None) -> tuple[str, str]:
    times = {}
    if extra_times:
        times.update(extra_times)
    if actual is not None:
        times["act_arr"] = actual
    xml = aidx_xml(
        event_id=event_id, sender=AUTH, revision=revision,
        effective_at=effective_at, identity_at=identity_at,
        carrier=k["carrier"], flight_no=k["flight_no"], suffix=k.get("suffix", ""),
        service_date=k["service_date"], dep=k["departure"], arr=k["arrival"],
        times=times)
    return (xml, "aidx_xml")


def roster(records: list[dict]) -> dict:
    return {"airport": "SHA",
            "roots": [
                {"root_id": r["root_id"], "root": r["key"],
                 "account": r["account"], "weight_kg": r["weight_kg"],
                 "category": r["category"]}
                for r in records]}


def rung(upto, mult):
    return {"upto_serial": upto, "ten_thousandths": mult}


def tariff(rates: list[dict], serials: list[dict],
           currency: str = "CNY") -> dict:
    return {"airport": "SHA", "currency": currency,
            "rates": rates, "serials": serials}


def rate(category, vf, vt, amount):
    return {"category": category, "valid_from": vf, "valid_to": vt,
            "min_per_unit": amount}


def ladder(account, rungs):
    return {"account": account, "rungs": rungs}


def write_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


# ------------------------------------------------------------- walkthrough
def build_walkthrough() -> None:
    base = os.path.join(BASE, "walkthrough")
    A, B, C = key("101"), key("102"), key("103")
    rels = [rel_initial("IL-A", "R-A", A, T0, AUTH, "J-A"),
            rel_initial("IL-B", "R-B", B, T0, AUTH, "J-B"),
            rel_initial("IL-C", "R-C", C, T0, AUTH, "J-C")]
    write_batch(base, 1, AUTH, {
        "identity_b1.json": (identity_file(rels), "identity_json"),
        # A: Jan 30 22:10 local
        "msg_A.xml": msg("E-A-1", A, "2026-01-30T22:10:00+08:00",
                         effective_at="2026-01-31T01:00:00+08:00"),
        # B: Jan 31 09:20 local
        "msg_B.xml": msg("E-B-1", B, "2026-01-31T09:20:00+08:00",
                         effective_at="2026-01-31T11:00:00+08:00"),
        # C: Feb 5 14:00 local
        "msg_C.xml": msg("E-C-1", C, "2026-02-05T14:00:00+08:00",
                         effective_at="2026-02-05T16:00:00+08:00"),
    })
    # Batch 2: only A is corrected (Jan 31 23:50 -> Feb 1 00:20, before C).
    # B and C have no new message at all.
    write_batch(base, 2, AUTH, {
        "msg_A_corr.xml": msg(
            "E-A-2", A, "2026-02-01T00:20:00+08:00", revision=2,
            effective_at="2026-02-02T09:00:00+08:00"),
    })
    roster_v1 = roster([
        {"root_id": "R-A", "key": A, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-B", "key": B, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-C", "key": C, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"},
    ])
    tariff_doc = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-A", [rung(1, 10000), rung(None, 5000)]),
                 ladder("ACCT-B", [rung(1, 10000), rung(None, 5000)])])
    roster_v2 = roster_v1
    roster_v3 = roster([
        {"root_id": "R-A", "key": A, "account": "ACCT-B",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-B", "key": B, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-C", "key": C, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"},
    ])
    write_json(os.path.join(base, "book.json"), {
        "airport": "SHA", "timezone": "Asia/Shanghai",
        "months": ["2026-01", "2026-02"], "currency": "CNY"})
    write_json(os.path.join(base, "roster_v1.json"), roster_v1)
    write_json(os.path.join(base, "roster_v2.json"), roster_v2)
    write_json(os.path.join(base, "roster_v3.json"), roster_v3)
    write_json(os.path.join(base, "tariff.json"), tariff_doc)


# -------------------------------------------------------------------- edge
def build_edge() -> None:
    base = os.path.join(BASE, "edge")
    KEYS = {
        "R-MULTI": key("110", "2026-01-01"),
        "R-REN": key("111", "2026-01-01"),
        "R-REN2": key("112", "2026-01-01"),
        "R-S1": key("120", "2026-01-01"),
        "R-S2": key("121", "2026-01-01"),
        "R-T": key("130", "2026-01-31", dep="URC"),
        "R-U1": key("140", "2026-02-01"),
        "R-U2": key("141", "2026-02-01"),
        "R-Z": key("150", "2026-01-01"),
        "R-W": key("160", "2026-01-01"),
        "R-H": key("170", "2026-01-01"),
        "R-D": key("180", "2026-01-01"),
        "R-DP": key("181", "2026-01-01"),
        "R-E": key("182", "2026-01-01"),
        "R-F": key("183", "2026-01-01", arr="PEK"),
        "R-O": key("184", "2026-03-01"),
    }
    rels = []
    for rid, k in KEYS.items():
        if rid == "R-REN2":
            continue  # R-REN2 is only a rename target of R-REN, not a root
        rels.append(rel_initial(f"IL-{rid}", rid, k, T0, AUTH, f"J-{rid}"))
    # rename chain for R-REN: 111 -> 112 (still ONE landing, not two)
    rels.append(rel_rename("RN-REN", KEYS["R-REN"], KEYS["R-REN2"],
                           "2026-01-01T00:00:00+08:00"))
    files = {"identity_b1.json": (identity_file(rels), "identity_json")}

    def add(name, k, actual, *, revision=1, effective_at,
            identity_at=T0, extra_times=None):
        files[name] = msg(name.replace(".xml", "").upper(), k, actual,
                          revision=revision, effective_at=effective_at,
                          identity_at=identity_at, extra_times=extra_times)

    # R-MULTI: three sparse messages, one landing. Old aliases don't add one.
    add("msg_multi_1.xml", KEYS["R-MULTI"], "2026-01-04T12:00:00+08:00",
        revision=1, effective_at="2026-01-04T13:00:00+08:00")
    add("msg_multi_2.xml", KEYS["R-MULTI"], "2026-01-04T12:00:00+08:00",
        revision=2, effective_at="2026-01-04T14:00:00+08:00")
    add("msg_multi_3.xml", KEYS["R-MULTI"], "2026-01-04T12:00:00+08:00",
        revision=3, effective_at="2026-01-04T15:00:00+08:00")
    # R-REN lands once under its retired alias too (message via new alias)
    add("msg_ren_old.xml", KEYS["R-REN"], "2026-01-04T13:00:00+08:00",
        revision=1, effective_at="2026-01-04T14:00:00+08:00")
    add("msg_ren_new.xml", KEYS["R-REN2"], "2026-01-04T13:00:00+08:00",
        revision=2, effective_at="2026-01-04T15:00:00+08:00",
        identity_at="2026-01-02T00:00:00+08:00")
    # same instant: R-S1 then R-S2 by ascending root id
    add("msg_s1.xml", KEYS["R-S1"], "2026-01-15T10:00:00+08:00",
        effective_at="2026-01-15T11:00:00+08:00")
    add("msg_s2.xml", KEYS["R-S2"], "2026-01-15T10:00:00+08:00",
        effective_at="2026-01-15T11:05:00+08:00")
    # time-zone month boundary: 2026-01-31T17:30Z = Feb 1 01:30 +08
    add("msg_t.xml", KEYS["R-T"], "2026-01-31T17:30:00Z",
        effective_at="2026-02-01T03:00:00+08:00")
    # local-date rate boundary (TURBO changes on 2026-02-03)
    add("msg_u1.xml", KEYS["R-U1"], "2026-02-02T12:00:00+08:00",
        effective_at="2026-02-02T13:00:00+08:00")
    add("msg_u2.xml", KEYS["R-U2"], "2026-02-03T00:30:00+08:00",
        effective_at="2026-02-03T02:00:00+08:00")
    # zero-amount priced landing
    add("msg_z.xml", KEYS["R-Z"], "2026-01-06T08:00:00+08:00",
        effective_at="2026-01-06T09:00:00+08:00")
    # 1500 kg -> 2 billable units
    add("msg_w.xml", KEYS["R-W"], "2026-01-07T08:00:00+08:00",
        effective_at="2026-01-07T09:00:00+08:00")
    # rounding half-up: 5 * 15000 / 10000 = 7.5 -> 8
    add("msg_h.xml", KEYS["R-H"], "2026-01-08T08:00:00+08:00",
        effective_at="2026-01-08T09:00:00+08:00")
    # R-D: scheduled/estimated only, NO actual -> unpriced (never filled in)
    add("msg_d.xml", KEYS["R-D"], None,
        effective_at="2026-01-09T09:00:00+08:00",
        extra_times={"sched_arr": "2026-01-09T09:00:00+08:00",
                     "est_arr": "2026-01-09T09:10:00+08:00"})
    # R-DP: priced landing in the same account as the unpriced roots
    add("msg_dp.xml", KEYS["R-DP"], "2026-01-10T08:00:00+08:00",
        effective_at="2026-01-10T09:00:00+08:00")
    # R-E: message visible before the cutoff but its actual arrival is later
    add("msg_e.xml", KEYS["R-E"], "2026-04-05T08:00:00+08:00",
        effective_at="2026-02-15T09:00:00+08:00")
    # R-O: in-book but March (outside settlement months) -> visible, not charged
    add("msg_o.xml", KEYS["R-O"], "2026-03-03T08:00:00+08:00",
        effective_at="2026-02-15T09:00:00+08:00")
    write_batch(base, 1, AUTH, files)

    roster_doc = roster([
        {"root_id": "R-MULTI", "key": KEYS["R-MULTI"], "account": "ACCT-M",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-REN", "key": KEYS["R-REN"], "account": "ACCT-M",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-S1", "key": KEYS["R-S1"], "account": "ACCT-S",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-S2", "key": KEYS["R-S2"], "account": "ACCT-S",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-T", "key": KEYS["R-T"], "account": "ACCT-T",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-U1", "key": KEYS["R-U1"], "account": "ACCT-U",
         "weight_kg": 1000, "category": "TURBO"},
        {"root_id": "R-U2", "key": KEYS["R-U2"], "account": "ACCT-U",
         "weight_kg": 1000, "category": "TURBO"},
        {"root_id": "R-Z", "key": KEYS["R-Z"], "account": "ACCT-Z",
         "weight_kg": 1000, "category": "ZERO"},
        {"root_id": "R-W", "key": KEYS["R-W"], "account": "ACCT-W",
         "weight_kg": 1500, "category": "JET"},
        {"root_id": "R-H", "key": KEYS["R-H"], "account": "ACCT-H",
         "weight_kg": 1000, "category": "ODD"},
        {"root_id": "R-D", "key": KEYS["R-D"], "account": "ACCT-D",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-DP", "key": KEYS["R-DP"], "account": "ACCT-D",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-E", "key": KEYS["R-E"], "account": "ACCT-D",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-F", "key": KEYS["R-F"], "account": "ACCT-D",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-X", "key": key("999"), "account": "ACCT-D",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-O", "key": KEYS["R-O"], "account": "ACCT-D",
         "weight_kg": 1000, "category": "JET"},
    ])
    tariff_doc = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000),
               rate("TURBO", "2026-01-01", "2026-02-03", 700),
               rate("TURBO", "2026-02-03", "2026-03-01", 900),
               rate("ZERO", "2026-01-01", "2026-03-01", 0),
               rate("ODD", "2026-01-01", "2026-03-01", 5)],
        serials=[
            ladder("ACCT-M", [rung(1, 10000), rung(None, 5000)]),
            ladder("ACCT-S", [rung(1, 10000), rung(None, 5000)]),
            ladder("ACCT-T", [rung(1, 10000), rung(None, 5000)]),
            ladder("ACCT-U", [rung(None, 10000)]),
            ladder("ACCT-Z", [rung(None, 10000)]),
            ladder("ACCT-W", [rung(1, 10000), rung(None, 7500)]),
            ladder("ACCT-H", [rung(None, 15000)]),
            ladder("ACCT-D", [rung(1, 10000), rung(None, 5000)]),
        ])
    write_json(os.path.join(base, "book.json"), {
        "airport": "SHA", "timezone": "Asia/Shanghai",
        "months": ["2026-01", "2026-02"], "currency": "CNY"})
    write_json(os.path.join(base, "roster.json"), roster_doc)
    write_json(os.path.join(base, "tariff.json"), tariff_doc)


# ----------------------------------------------------------------- invalid
def build_invalid() -> None:
    base = os.path.join(BASE, "invalid")
    good_key = key("201")
    good_roster = roster([
        {"root_id": "R-I1", "key": good_key, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"}])
    good_tariff = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-A", [rung(None, 10000)])])
    write_json(os.path.join(base, "book.json"), {
        "airport": "SHA", "timezone": "Asia/Shanghai",
        "months": ["2026-01", "2026-02"], "currency": "CNY"})
    write_json(os.path.join(base, "good_roster.json"), good_roster)
    write_json(os.path.join(base, "good_tariff.json"), good_tariff)

    def duo(**over):
        r = json.loads(json.dumps(good_roster))
        r["roots"][0].update(over)
        return r

    dup = roster([
        {"root_id": "R-I1", "key": good_key, "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"},
        {"root_id": "R-I1", "key": key("202"), "account": "ACCT-A",
         "weight_kg": 1000, "category": "JET"}])
    write_json(os.path.join(base, "dup_root_roster.json"), dup)
    write_json(os.path.join(base, "bad_weight_roster.json"),
               duo(weight_kg=0))
    write_json(os.path.join(base, "float_weight_roster.json"),
               duo(weight_kg=1000.5))
    write_json(os.path.join(base, "bad_account_roster.json"),
               duo(account="  "))

    t_overlap = tariff(
        rates=[rate("JET", "2026-01-01", "2026-02-15", 1000),
               rate("JET", "2026-02-10", "2026-03-01", 900)],
        serials=[ladder("ACCT-A", [rung(None, 10000)])])
    write_json(os.path.join(base, "overlap_tariff.json"), t_overlap)
    t_nofallback = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-A", [rung(5, 10000)])])
    write_json(os.path.join(base, "nofallback_tariff.json"), t_nofallback)
    t_dup_ladder = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-A", [rung(None, 10000)]),
                 ladder("ACCT-A", [rung(None, 9000)])])
    write_json(os.path.join(base, "dupladder_tariff.json"), t_dup_ladder)
    t_nocat = tariff(
        rates=[rate("OTHER", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-A", [rung(None, 10000)])])
    write_json(os.path.join(base, "nocat_tariff.json"), t_nocat)
    t_noacct = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-OTHER", [rung(None, 10000)])])
    write_json(os.path.join(base, "noacct_tariff.json"), t_noacct)
    t_neg = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", -1)],
        serials=[ladder("ACCT-A", [rung(None, 10000)])])
    write_json(os.path.join(base, "negative_rate_tariff.json"), t_neg)
    t_cur = tariff(
        rates=[rate("JET", "2026-01-01", "2026-03-01", 1000)],
        serials=[ladder("ACCT-A", [rung(None, 10000)])], currency="USD")
    write_json(os.path.join(base, "currency_tariff.json"), t_cur)
    with open(os.path.join(base, "malformed.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{ not json ]\n")


# ------------------------------------------------------- hand expectations
WALKTHROUGH_EXPECTED = {
    "description": "Hand-computed expectations for the landing mart "
                   "walkthrough. Derived by hand from the fixtures; the "
                   "checker never calls the pricing implementation to "
                   "produce them.",
    "book": {"airport": "SHA", "timezone": "Asia/Shanghai",
             "months": ["2026-01", "2026-02"], "currency": "CNY"},
    "phases": [
        {
            "name": "v1 baseline (batch 1)",
            "base_version": None,
            "receipt_batch": 1,
            "cutoff": "2026-02-10T00:00:00+08:00",
            "roster": "roster_v1.json",
            "priced_by_root": {
                "R-A": {"account": "ACCT-A", "month": "2026-01", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
                "R-B": {"account": "ACCT-A", "month": "2026-01", "serial": 2,
                        "units": 1, "rate": 1000, "mult": 5000,
                        "amount_minor": 500},
                "R-C": {"account": "ACCT-A", "month": "2026-02", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
            },
            "unpriced": {},
            "out_of_range": [],
            "effective_total_minor": 2500,
            "entries": [
                {"kind": "positive", "root_id": "R-A", "account": "ACCT-A",
                 "month": "2026-01", "amount_minor": 1000},
                {"kind": "positive", "root_id": "R-B", "account": "ACCT-A",
                 "month": "2026-01", "amount_minor": 500},
                {"kind": "positive", "root_id": "R-C", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": 1000},
            ],
        },
        {
            "name": "v2 A corrected from January into February, before C; "
                    "B and C have no new message",
            "base_version": 1,
            "receipt_batch": 2,
            "cutoff": "2026-02-10T00:00:00+08:00",
            "roster": "roster_v2.json",
            "priced_by_root": {
                "R-A": {"account": "ACCT-A", "month": "2026-02", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
                "R-B": {"account": "ACCT-A", "month": "2026-01", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
                "R-C": {"account": "ACCT-A", "month": "2026-02", "serial": 2,
                        "units": 1, "rate": 1000, "mult": 5000,
                        "amount_minor": 500},
            },
            "unpriced": {},
            "out_of_range": [],
            "effective_total_minor": 2500,
            "entries": [
                {"kind": "reversal", "root_id": "R-A", "account": "ACCT-A",
                 "month": "2026-01", "amount_minor": -1000},
                {"kind": "reversal", "root_id": "R-B", "account": "ACCT-A",
                 "month": "2026-01", "amount_minor": -500},
                {"kind": "reversal", "root_id": "R-C", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": -1000},
                {"kind": "positive", "root_id": "R-A", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": 1000},
                {"kind": "positive", "root_id": "R-B", "account": "ACCT-A",
                 "month": "2026-01", "amount_minor": 1000},
                {"kind": "positive", "root_id": "R-C", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": 500},
            ],
            "entry_net_minor": 0,
            "why_entries_if_net_zero": (
                "The total stays 2500 because A moved from January (serial 1, "
                "1000) to February (serial 1, 1000): its amount is unchanged, "
                "but its original attribution month changes, so the January "
                "posting must be reversed and re-booked in February. The move "
                "also re-orders February: A takes serial 1 (1000) and C falls "
                "to serial 2 (500), a -500 change; January keeps only B, which "
                "rises from serial 2 (500) to serial 1 (1000), +500. The two "
                "collateral changes cancel, but each is a real change to the "
                "original account/month/amount and must be journalled. A pure "
                "net-zero check would hide both re-pricings."),
        },
        {
            "name": "v3 A reassigned to ACCT-B; C re-serialises in ACCT-A",
            "base_version": 2,
            "receipt_batch": 2,
            "cutoff": "2026-02-10T00:00:00+08:00",
            "roster": "roster_v3.json",
            "priced_by_root": {
                "R-A": {"account": "ACCT-B", "month": "2026-02", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
                "R-B": {"account": "ACCT-A", "month": "2026-01", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
                "R-C": {"account": "ACCT-A", "month": "2026-02", "serial": 1,
                        "units": 1, "rate": 1000, "mult": 10000,
                        "amount_minor": 1000},
            },
            "unpriced": {},
            "out_of_range": [],
            "effective_total_minor": 3000,
            "entries": [
                {"kind": "reversal", "root_id": "R-A", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": -1000},
                {"kind": "reversal", "root_id": "R-C", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": -500},
                {"kind": "positive", "root_id": "R-A", "account": "ACCT-B",
                 "month": "2026-02", "amount_minor": 1000},
                {"kind": "positive", "root_id": "R-C", "account": "ACCT-A",
                 "month": "2026-02", "amount_minor": 1000},
            ],
            "entry_net_minor": 500,
        },
    ],
    "stability_checks": {
        "replay_v2_after_v3_is_rejected": True,
        "same_request_replay_returns_version": 2,
        "v1_lines_unchanged_after_v2_v3": True,
        "v2_net_zero_entries": 6,
    },
}

EDGE_EXPECTED = {
    "description": "Hand-computed expectations for the landing mart edge "
                   "fixtures (batch 1, cutoff 2026-03-10T00:00:00+08:00).",
    "cutoff": "2026-03-10T00:00:00+08:00",
    "receipt_batch": 1,
    "priced_by_root": {
        "R-MULTI": {"account": "ACCT-M", "month": "2026-01", "serial": 1,
                    "units": 1, "rate": 1000, "mult": 10000,
                    "amount_minor": 1000,
                    "note": "three reports of one landing count once"},
        "R-REN": {"account": "ACCT-M", "month": "2026-01", "serial": 2,
                  "units": 1, "rate": 1000, "mult": 5000,
                  "amount_minor": 500,
                  "note": "old and new alias are one operating root"},
        "R-S1": {"account": "ACCT-S", "month": "2026-01", "serial": 1,
                 "units": 1, "rate": 1000, "mult": 10000,
                 "amount_minor": 1000},
        "R-S2": {"account": "ACCT-S", "month": "2026-01", "serial": 2,
                 "units": 1, "rate": 1000, "mult": 5000,
                 "amount_minor": 500,
                 "note": "same instant as R-S1; R-S1 < R-S2 by root id"},
        "R-T": {"account": "ACCT-T", "month": "2026-02", "serial": 1,
                "units": 1, "rate": 1000, "mult": 10000,
                "amount_minor": 1000,
                "note": "17:30Z on Jan 31 is Feb 1 01:30 at +08:00"},
        "R-U1": {"account": "ACCT-U", "month": "2026-02", "serial": 1,
                 "units": 1, "rate": 700, "mult": 10000,
                 "amount_minor": 700},
        "R-U2": {"account": "ACCT-U", "month": "2026-02", "serial": 2,
                 "units": 1, "rate": 900, "mult": 10000,
                 "amount_minor": 900,
                 "note": "local date 2026-02-03 is in the second interval"},
        "R-Z": {"account": "ACCT-Z", "month": "2026-01", "serial": 1,
                "units": 1, "rate": 0, "mult": 10000, "amount_minor": 0,
                "note": "priced at zero keeps a posting identity"},
        "R-W": {"account": "ACCT-W", "month": "2026-01", "serial": 1,
                "units": 2, "rate": 1000, "mult": 10000,
                "amount_minor": 2000,
                "note": "1500 kg -> 2 units (ceil)"},
        "R-H": {"account": "ACCT-H", "month": "2026-01", "serial": 1,
                "units": 1, "rate": 5, "mult": 15000, "amount_minor": 8,
                "note": "5 * 15000 / 10000 = 7.5 rounds half-up to 8"},
        "R-DP": {"account": "ACCT-D", "month": "2026-01", "serial": 1,
                 "units": 1, "rate": 1000, "mult": 10000,
                 "amount_minor": 1000},
    },
    "unpriced": {
        "R-D": "no_definite_actual",
        "R-E": "actual_after_cutoff",
        "R-F": "arrival_airport_mismatch",
        "R-X": "unknown_root",
    },
    "out_of_range": {"R-O": "month_outside_book"},
    "effective_total_minor": 8608,
}


def main() -> None:
    os.makedirs(BASE, exist_ok=True)
    build_walkthrough()
    build_edge()
    build_invalid()
    exp = os.path.join(BASE, "expected")
    write_json(os.path.join(exp, "walkthrough_expected.json"),
               WALKTHROUGH_EXPECTED)
    write_json(os.path.join(exp, "edge_expected.json"), EDGE_EXPECTED)
    print(json.dumps({"fixture_root": BASE,
                      "suites": ["walkthrough:1-2", "edge:1", "invalid"],
                      "expected": ["walkthrough", "edge"]}, indent=2))


if __name__ == "__main__":
    main()
