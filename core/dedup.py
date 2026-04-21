"""
core/dedup.py
Prevents sending duplicate notifications for the same ES hit.
Uses in-memory set with TTL cleanup.
"""

import time
import logging

logger = logging.getLogger(__name__)


class DedupTracker:
    def __init__(self, window_seconds: int = 300):
        self.window = window_seconds
        # {hit_id: timestamp_when_first_seen}
        self._seen: dict[str, float] = {}

    def is_new(self, hit_id: str) -> bool:
        """Return True if this hit_id hasn't been seen within the dedup window."""
        now = time.time()
        self._cleanup(now)
        if hit_id in self._seen:
            return False
        self._seen[hit_id] = now
        return True

    def _cleanup(self, now: float):
        expired = [k for k, t in self._seen.items() if now - t > self.window]
        for k in expired:
            del self._seen[k]
        if expired:
            logger.debug(f"Dedup: cleared {len(expired)} expired entries.")
