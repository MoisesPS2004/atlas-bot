"""
Hueco G — bound in-memory conversation state (deep module).

Owns the LLM's short-term conversation context that used to live in `bot.py`'s
module-global `_history` dict. The state stays VOLATILE by conscious decision
(Session 9 alignment): it is ephemeral scratch for the agentic loop, not a system
of record — the authoritative state lives in aquarela.db via the engine. We trade
conversation continuity across restarts for a simple, self-bounding store that
never touches aquarela's schema (Hueco A contract).

The public surface is intentionally narrow (a deep module):

    get_history(user_id)                -> list[dict]   snapshot copy
    append_history(user_id, role, msg)  -> None
    lock(user_id)                       -> asyncio.Lock per-user turn serializer

Everything else — the `deque(maxlen)` length cap, the `OrderedDict` LRU over
users, and lazy TTL expiry on a `time.monotonic()` clock — is hidden.

Concurrency note: get_history/append_history are synchronous, so they are atomic
with respect to the single-threaded asyncio event loop; they need no lock. The
per-user `asyncio.Lock` exists to serialize the *whole turn* (read history →
await the model → append reply) so a burst of messages from one telegram_id
cannot interleave and corrupt turn order.
"""
import asyncio
import time
from collections import OrderedDict, deque
from typing import Callable


class ConversationStore:
    """Bound in-memory per-user conversation history with LRU + TTL eviction."""

    def __init__(
        self,
        *,
        max_history: int = 10,
        max_users: int = 500,
        ttl_seconds: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_history = max_history
        self._max_users = max_users
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        # user_id -> {"turns": deque(maxlen=max_history), "last_access": float}
        # OrderedDict preserves access order for LRU: most-recently used at the end.
        self._entries: "OrderedDict[int, dict]" = OrderedDict()
        # Per-user locks live in their own map; cleaned up alongside LRU/TTL
        # eviction, but never while a lock is held.
        self._locks: dict[int, asyncio.Lock] = {}

    # ── Public interface ─────────────────────────────────────────────────────
    def get_history(self, user_id: int) -> list[dict]:
        """Return a fresh list of turn dicts for the user ([] if new/expired)."""
        entry = self._live_entry(user_id)
        if entry is None:
            return []
        self._entries.move_to_end(user_id)  # mark as most-recently used
        entry["last_access"] = self._clock()
        # New list of new dicts: callers may mutate the result freely.
        return [dict(turn) for turn in entry["turns"]]

    def append_history(self, user_id: int, role: str, content: str) -> None:
        """Append a turn, capping length and refreshing recency/TTL."""
        entry = self._live_entry(user_id)
        if entry is None:
            entry = {
                "turns": deque(maxlen=self._max_history),
                "last_access": self._clock(),
            }
            self._entries[user_id] = entry
        entry["turns"].append({"role": role, "content": content})
        entry["last_access"] = self._clock()
        self._entries.move_to_end(user_id)  # mark as most-recently used
        self._evict_over_capacity()

    def lock(self, user_id: int) -> asyncio.Lock:
        """Return the per-user turn lock, creating it on first use."""
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    # ── Internals ────────────────────────────────────────────────────────────
    def _live_entry(self, user_id: int) -> dict | None:
        """Return the user's entry if present and not TTL-expired, else None.

        Lazily evicts an expired entry so a subsequent read/write starts fresh.
        """
        entry = self._entries.get(user_id)
        if entry is None:
            return None
        if self._clock() - entry["last_access"] > self._ttl_seconds:
            self._forget(user_id)
            return None
        return entry

    def _evict_over_capacity(self) -> None:
        """Drop least-recently-used users until within max_users."""
        while len(self._entries) > self._max_users:
            oldest_id, _ = self._entries.popitem(last=False)
            self._drop_lock(oldest_id)

    def _forget(self, user_id: int) -> None:
        self._entries.pop(user_id, None)
        self._drop_lock(user_id)

    def _drop_lock(self, user_id: int) -> None:
        """Discard a user's lock, but never one that is currently held."""
        lock = self._locks.get(user_id)
        if lock is not None and not lock.locked():
            del self._locks[user_id]
