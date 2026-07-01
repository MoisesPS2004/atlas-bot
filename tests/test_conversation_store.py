"""
Vertical Slice — Hueco G: volatile in-memory conversation state.

`bot.py` keeps the LLM's short-term conversation context in a module-global
`dict[int, list[dict]]` (`_history`). The Session-9 diagnosis flagged three
grietas: it is lost on every service restart, it grows without eviction (a new
`telegram_id` is a key that never leaves), and a burst of concurrent messages
from the same user can interleave across the `await` of the agentic loop and
corrupt the turn order sent to the model.

DECISION (alignment phase, Session 9 / Hueco G): we deliberately keep this state
VOLATILE and choose *bound in-memory* over real persistence. `_history` is
ephemeral conversational scratch, not a system of record — the authoritative
state lives in aquarela.db via the engine. We consciously sacrifice conversation
continuity across restarts; in exchange we avoid coupling atlas-bot to aquarela's
schema (Hueco A contract) and avoid standing up a second store.

CONTRACT (Shared Design Concept, approved): a deep module `conversation_store`
exposes a `ConversationStore` behind a narrow interface —

    get_history(user_id)                -> list[dict]   (snapshot copy)
    append_history(user_id, role, msg)  -> None
    lock(user_id)                       -> asyncio.Lock (per-user serializer)

Internals (hidden): a `deque(maxlen=max_history)` per user caps length; an
`OrderedDict` gives LRU eviction over the number of users; lazy TTL expiry on a
`time.monotonic()` clock drops idle conversations. The per-user `asyncio.Lock`
does NOT guard the (synchronous, event-loop-atomic) append/get — it serializes
the *whole turn* that spans the awaited model call, so two concurrent messages
from one telegram_id cannot interleave their history.

Testability seam: `__init__` accepts an injectable `clock: () -> float`
defaulting to `time.monotonic`, so lazy-TTL expiry is exercised deterministically
without sleeping.

These tests are EXPECTED to be RED on first run: `conversation_store` does not
exist yet (ModuleNotFoundError on import).
"""
import asyncio

import pytest

from conversation_store import ConversationStore


class _FakeClock:
    """Controllable monotonic clock so TTL is testable without wall-clock sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# ── Cap de longitud (max_history) ────────────────────────────────────────────
def test_length_cap_evicts_oldest_turns():
    """A user exceeding max_history keeps only the most recent turns."""
    store = ConversationStore(max_history=2)

    store.append_history(1, "user", "m1")
    store.append_history(1, "assistant", "r1")
    store.append_history(1, "user", "m2")

    assert store.get_history(1) == [
        {"role": "assistant", "content": "r1"},
        {"role": "user", "content": "m2"},
    ]


def test_get_history_returns_a_snapshot_copy():
    """Mutating the returned list must not corrupt internal state."""
    store = ConversationStore(max_history=5)
    store.append_history(1, "user", "m1")

    snapshot = store.get_history(1)
    snapshot.append({"role": "user", "content": "injected"})

    assert store.get_history(1) == [{"role": "user", "content": "m1"}]


# ── Evicción LRU (max_users) ─────────────────────────────────────────────────
def test_lru_evicts_least_recently_accessed_user():
    """When max_users is exceeded, the least-recently-accessed user is dropped."""
    store = ConversationStore(max_users=2)

    store.append_history(1, "user", "a")
    store.append_history(2, "user", "b")

    # Touch user 1 so user 2 becomes the least-recently-accessed.
    store.get_history(1)

    # Adding a third user exceeds max_users=2 -> user 2 is evicted.
    store.append_history(3, "user", "c")

    assert store.get_history(2) == []  # evicted -> treated as new
    assert store.get_history(1) == [{"role": "user", "content": "a"}]
    assert store.get_history(3) == [{"role": "user", "content": "c"}]


# ── TTL perezoso (time.monotonic) ────────────────────────────────────────────
def test_ttl_lazy_expiry_treats_idle_user_as_new():
    """A user idle longer than ttl_seconds is read back as empty history."""
    clock = _FakeClock()
    store = ConversationStore(ttl_seconds=100, clock=clock)

    store.append_history(1, "user", "hello")
    clock.advance(101)

    assert store.get_history(1) == []


def test_ttl_keeps_history_within_window():
    """Activity within the TTL window preserves the conversation."""
    clock = _FakeClock()
    store = ConversationStore(ttl_seconds=100, clock=clock)

    store.append_history(1, "user", "hello")
    clock.advance(50)

    assert store.get_history(1) == [{"role": "user", "content": "hello"}]


# ── Aislamiento de concurrencia (locks por usuario) ──────────────────────────
def test_lock_is_stable_per_user_and_isolated_across_users():
    """lock(user) returns the same object for a user and a distinct one per user."""
    store = ConversationStore()

    lock_1a = store.lock(1)
    lock_1b = store.lock(1)
    lock_2 = store.lock(2)

    assert isinstance(lock_1a, asyncio.Lock)
    assert lock_1a is lock_1b        # stable per user
    assert lock_1a is not lock_2     # isolated across users


async def test_locks_do_not_block_across_users():
    """Holding one user's lock must not block another user's lock."""
    store = ConversationStore()
    lock_1 = store.lock(1)
    lock_2 = store.lock(2)

    async with lock_1:
        assert not lock_2.locked()
        async with lock_2:
            assert lock_2.locked()
