"""Handover manifest construction helpers.

A manifest names the sender, the next continuous positive integer
``receipt_batch`` and, for every file in the batch, its relative path,
sha256 digest, byte size and kind.  The warehouse only ever accepts a batch
when every listed file matches its digest and size, and publishes the batch
atomically — partial batches are never visible.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Iterable

from .storage import sha256_file


def build_manifest(files_root: str, rel_paths: Iterable[str], sender: str,
                   receipt_batch: int, kinds: dict[str, str] | None = None) -> dict:
    kinds = kinds or {}
    entries = []
    for rel in sorted(rel_paths):
        full = os.path.join(files_root, rel)
        digest, size = sha256_file(full)
        kind = kinds.get(rel)
        if kind is None:
            kind = "identity_json" if rel.endswith(".json") else "aidx_xml"
        entries.append({"path": rel, "sha256": digest,
                        "size_bytes": size, "kind": kind})
    return {
        "sender": sender,
        "receipt_batch": receipt_batch,
        "file_count": len(entries),
        "files": entries,
    }


def write_manifest(manifest: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
