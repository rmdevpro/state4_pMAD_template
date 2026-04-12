"""Shared utilities for the Context Broker."""

import hashlib


def stable_lock_id(key: str) -> int:
    """Generate a deterministic positive bigint from a string key.

    Used for Postgres advisory locks. Unlike Python's built-in hash(),
    this produces the same value across different processes and restarts
    (Python's hash() is randomized per process via PYTHONHASHSEED).
    """
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
