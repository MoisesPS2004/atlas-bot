"""
Vertical Slice: the access-control trust boundary — _check_access(telegram_id).

These tests lock the SECURITY CONTRACT agreed during the alignment phase ("Grill Me"):

  1. Precedence  — a telegram_id present in BOTH tables resolves to ("admin", role).
  2. Asymmetry   — admin membership is binary (presence == access; there is no
                   `active` column for admins); a volunteer with active = 0 is denied.
  3. Roles       — the role string is propagated verbatim for all 3 valid roles
                   ('marielle', 'thibaut', 'manager_temp').
  4. Fail-closed — any DB/SQLite exception denies access (returns None).
  5. Unknown     — an unregistered telegram_id returns None.

NOTE (test-debt retrofit): _check_access already exists and already honors this
contract. These are characterization tests that pin the contract as a regression
net, so they are EXPECTED to run GREEN on first execution — that GREEN is the proof
the door was already built to spec. A RED here would mean the live code violates an
invariant we agreed on, which would open the REFACTOR card.
"""
import sqlite3

import pytest

# conftest.py stubs out .env / openai / telegram before this import.
import bot


# ─── Fixture: an isolated, real SQLite DB matching the production schema ────────

@pytest.fixture
def access_db(tmp_path, monkeypatch):
    """
    Build a throwaway SQLite file with the real admins/volunteers schema and
    point bot._DB_PATH at it. Yields the live connection so each test can seed
    exactly the rows it needs. The DB is discarded with tmp_path after the test.
    """
    db_path = tmp_path / "test_aquarela.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE admins (
            id          INTEGER PRIMARY KEY,
            telegram_id TEXT    NOT NULL UNIQUE,
            role        TEXT    NOT NULL CHECK(role IN ('marielle','thibaut','manager_temp')),
            added_by    TEXT,
            created_at  TEXT    NOT NULL
        );
        CREATE TABLE volunteers (
            id          INTEGER PRIMARY KEY,
            telegram_id TEXT    NOT NULL UNIQUE,
            name        TEXT    NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
            language    TEXT,
            country     TEXT,
            created_at  TEXT    NOT NULL
        );
        """
    )
    conn.commit()
    monkeypatch.setattr(bot, "_DB_PATH", str(db_path))
    yield conn
    conn.close()


def _add_admin(conn, telegram_id, role="manager_temp"):
    conn.execute(
        "INSERT INTO admins (telegram_id, role, created_at) VALUES (?, ?, ?)",
        (str(telegram_id), role, "2026-06-30T00:00:00Z"),
    )
    conn.commit()


def _add_volunteer(conn, telegram_id, vol_id, active=1, name="Test Vol"):
    conn.execute(
        "INSERT INTO volunteers (id, telegram_id, name, active, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (vol_id, str(telegram_id), name, active, "2026-06-30T00:00:00Z"),
    )
    conn.commit()


# ─── Invariant 1: precedence — admin wins on dual membership ───────────────────

def test_dual_membership_resolves_to_admin(access_db):
    """SDC #1: a telegram_id in BOTH tables must resolve to admin (max privilege)."""
    TID = 8632082731
    _add_admin(access_db, TID, role="marielle")
    _add_volunteer(access_db, TID, vol_id=42, active=1)

    result = bot._check_access(TID)

    assert result == ("admin", "marielle"), (
        f"PRECEDENCE: a dual-member must resolve to admin, got {result!r}. "
        f"A leader must never lose admin tools by also being an active volunteer."
    )


# ─── Invariant 2: asymmetry — admin binary, volunteer gated by active ──────────

def test_active_volunteer_is_admitted(access_db):
    _add_volunteer(access_db, 111, vol_id=7, active=1)
    assert bot._check_access(111) == ("volunteer", 7)


def test_inactive_volunteer_is_denied(access_db):
    """SDC #2: volunteer membership is gated by active = 1; active = 0 → None."""
    _add_volunteer(access_db, 222, vol_id=8, active=0)
    assert bot._check_access(222) is None


def test_admin_membership_is_binary_by_presence(access_db):
    """SDC #2: there is no `active` concept for admins; presence alone grants access."""
    _add_admin(access_db, 333, role="thibaut")
    assert bot._check_access(333) == ("admin", "thibaut")


# ─── Invariant 3: all 3 valid roles propagate verbatim ─────────────────────────

@pytest.mark.parametrize("role", ["marielle", "thibaut", "manager_temp"])
def test_admin_role_is_propagated_verbatim(access_db, role):
    """The CHECK-constrained role enum must reach the caller untransformed."""
    _add_admin(access_db, 444, role=role)
    assert bot._check_access(444) == ("admin", role)


# ─── Invariant 4: fail-closed on any DB error ──────────────────────────────────

def test_db_exception_denies_access(access_db, monkeypatch):
    """A SQLite failure must DENY (return None), never leak access."""
    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bot.sqlite3, "connect", _boom)
    assert bot._check_access(999) is None


# ─── Invariant 5: unknown telegram_id ──────────────────────────────────────────

def test_unknown_telegram_id_returns_none(access_db):
    """An id present in neither table is denied."""
    assert bot._check_access(1234567) is None
