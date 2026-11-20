#!/usr/bin/env python3
"""Identity-graph scale test: long chains and wide forks must not enumerate
exponentially many alias paths or blow resident memory.

The graph follows rename/codeshare edges on demand with a visited guard.
This test builds a 20,000-edge rename chain and a 10,000-way fork, then binds
keys at both ends while measuring memory and time.
"""
from __future__ import annotations

import os
import sys
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from legfact.identity import (Codeshare, IdentityGraph, InitialLeg,  # noqa: E402
                              OpKey, Rename)
from legfact.timeutil import parse_offset_datetime  # noqa: E402

T = "2026-11-30T08:00:00+08:00"


def key(fno: str) -> OpKey:
    return OpKey("CA", fno, "", "2026-11-30", "PEK", "SHA", "OP")


def main() -> int:
    at = parse_offset_datetime(T)
    n_chain = 20000
    n_fork = 10000

    initials = [InitialLeg(
        root_id="R0", key=key("100000"), valid_from=at,
        sender="CA-OCC", journey_id="J0", source={})]
    # long single chain: 100000 -> 100001 -> ... -> 119999
    renames = [
        Rename(rid=f"RC{i}", old_key=key(str(100000 + i)),
               new_key=key(str(100001 + i)), effective_at=at, source={})
        for i in range(n_chain)]
    # wide fork off a second root: 200000 -> 200001..210000
    initials.append(InitialLeg(
        root_id="RF", key=key("200000"), valid_from=at,
        sender="CA-OCC", journey_id="JF", source={}))
    renames += [
        Rename(rid=f"RF{i}", old_key=key("200000"),
               new_key=key(str(200001 + i)), effective_at=at, source={})
        for i in range(n_fork)]

    tracemalloc.start()
    t0 = time.perf_counter()
    graph = IdentityGraph.build(initials, renames, [])
    build_s = time.perf_counter() - t0
    _, peak_build = tracemalloc.get_traced_memory()

    # bind the far end of the chain (must walk back ONCE, not enumerate)
    t0 = time.perf_counter()
    b_far = graph.bind_operating(key(str(100000 + n_chain)), at)
    far_s = time.perf_counter() - t0

    # bind the fork head (ambiguous, but candidate set must stay bounded)
    b_fork_head = graph.bind_operating(key("200000"), at)
    # bind one fork leaf
    b_leaf = graph.bind_operating(key("200500"), at)
    _, peak_all = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    ok = True
    ok &= b_far.status == "bound" and b_far.root_id == "R0"
    # the forked head itself is ambiguous; we refuse to pick a successor
    ok &= b_fork_head.status == "ambiguous"
    # a specific leaf has one back-path to the declared root RF, so it binds
    # deterministically while the head fork remains unresolved
    ok &= b_leaf.status == "bound" and b_leaf.root_id == "RF"
    # chain length is linear in walked edges, never exponential
    ok &= len(b_far.chain) == n_chain + 1
    # memory stays in the low-tens-of-MB despite 30k edges
    ok &= peak_all < 120 * 1024 * 1024

    import json
    print(json.dumps({
        "rename_edges": len(renames),
        "build_seconds": round(build_s, 3),
        "far_end_bind_seconds": round(far_s, 3),
        "far_chain_length": len(b_far.chain),
        "far_status": b_far.status,
        "fork_head_status": b_fork_head.status,
        "leaf_status": b_leaf.status,
        "peak_bytes": peak_all,
        "peak_mb": round(peak_all / 1048576, 2),
        "pass": ok,
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
