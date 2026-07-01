"""
Vertical Slice: the tool-dispatch trust boundary — call_grok_core.authorize()
and call_grok_core.pin_identity() (Hueco B / B.1).

These lock the SECURITY CONTRACT agreed during the B.1 alignment ("Grill Me"):

  1. Fail-closed  — a tool_name outside KNOWN_TOOL_NAMES is denied.
  2. Role-blind fail-closed — the fail-closed check above applies BEFORE the
     admin bypass; an unregistered name is denied even for an admin.
  3. Admin bypass — a known admin has no restriction on any known tool.
  4. Notify family — notify_admins and notify_volunteer are admin-only for
     every non-admin user_type, with no exception (this is the fix for the
     notify_admins gap documented in B.0's characterization suite).
  5. Admin-only tools — the pre-existing _ADMIN_ONLY_TOOLS set stays
     admin-only for non-admins.
  6. Open tools — everything else is open to any known user_type.
  7. Exact error messages — the driver in bot.py forwards these verbatim to
     the model as the tool_result; a wrong string is a silent contract break.
  8. Registry disjointness — ADMIN_ONLY_TOOLS, NOTIFY_TOOLS and OPEN_TOOLS
     don't overlap and together are exactly KNOWN_TOOL_NAMES (14 tools).
  9. pin_identity pins volunteer_id only for volunteer + save_preferences,
     without mutating the input dict.

0 I/O, 0 mocks needed — these call the pure functions directly.
"""
import pytest

from call_grok_core import (
    authorize,
    pin_identity,
    ADMIN_ONLY_TOOLS,
    NOTIFY_TOOLS,
    OPEN_TOOLS,
    KNOWN_TOOL_NAMES,
)


# ─── Invariant 1 & 2: fail-closed, before the admin bypass ─────────────────────

@pytest.mark.parametrize("user_type", ["volunteer", "admin"])
def test_unknown_tool_name_is_denied_regardless_of_role(user_type):
    """SDC #1/#2: an unregistered tool_name is denied even for an admin."""
    allowed, error = authorize(user_type, "totally_unknown_tool")
    assert allowed is False
    assert error == "permission denied: unknown tool"


# ─── Invariant 3: admin bypass on every known tool ─────────────────────────────

@pytest.mark.parametrize("tool_name", sorted(KNOWN_TOOL_NAMES))
def test_admin_is_authorized_for_every_known_tool(tool_name):
    """SDC #3: an admin has no restriction on any registered tool."""
    assert authorize("admin", tool_name) == (True, None)


# ─── Invariant 4: notify_* is admin-only, no exception ─────────────────────────

@pytest.mark.parametrize("tool_name", sorted(NOTIFY_TOOLS))
def test_volunteer_is_denied_for_every_notify_tool(tool_name):
    """SDC #4: closes the notify_admins gap from B.0 — same rule for both."""
    allowed, error = authorize("volunteer", tool_name)
    assert allowed is False
    assert error == "permission denied"


# ─── Invariant 5: pre-existing admin-only tools stay admin-only ───────────────

@pytest.mark.parametrize("tool_name", sorted(ADMIN_ONLY_TOOLS))
def test_volunteer_is_denied_for_every_admin_only_tool(tool_name):
    """SDC #5: unchanged behavior, now enforced by authorize() instead of
    an inline bot.py membership check."""
    allowed, error = authorize("volunteer", tool_name)
    assert allowed is False
    assert error == "permission denied: this action is for admins only"


# ─── Invariant 6: open tools stay open to volunteers ───────────────────────────

@pytest.mark.parametrize("tool_name", sorted(OPEN_TOOLS))
def test_volunteer_is_authorized_for_every_open_tool(tool_name):
    """SDC #6: authorize() must not restrict tools that were never gated."""
    assert authorize("volunteer", tool_name) == (True, None)


# ─── Invariant 8: registry disjointness and completeness ──────────────────────

def test_tool_tiers_are_disjoint_and_complete():
    """SDC #8: no tool_name is double-classified, and the union is exactly
    the 14-tool registry — a silent gap here would make authorize() treat a
    real tool as unknown (fail-closed, safe) or skip a tier's rule (unsafe)."""
    assert ADMIN_ONLY_TOOLS & NOTIFY_TOOLS == set()
    assert ADMIN_ONLY_TOOLS & OPEN_TOOLS == set()
    assert NOTIFY_TOOLS & OPEN_TOOLS == set()
    assert ADMIN_ONLY_TOOLS | NOTIFY_TOOLS | OPEN_TOOLS == KNOWN_TOOL_NAMES
    assert len(KNOWN_TOOL_NAMES) == 14


# ─── Invariant 9: pin_identity ──────────────────────────────────────────────────

def test_pin_identity_overrides_volunteer_id_for_volunteer_save_preferences():
    """SDC #9: the LLM-supplied volunteer_id is never trusted for a volunteer
    self-service call — it's replaced by the authenticated internal_id."""
    original = {"volunteer_id": 999, "data": "{}"}
    result = pin_identity("volunteer", "save_preferences", original, internal_id=42)

    assert result["volunteer_id"] == 42
    assert original == {"volunteer_id": 999, "data": "{}"}, "must not mutate the input dict"


def test_pin_identity_does_not_pin_for_admin_save_preferences():
    """An admin may set preferences on a volunteer's behalf with an explicit
    volunteer_id — pin_identity must leave that alone."""
    original = {"volunteer_id": 999, "data": "{}"}
    result = pin_identity("admin", "save_preferences", original, internal_id="marielle")
    assert result == original


def test_pin_identity_is_a_passthrough_for_other_tools():
    """No pinning rule exists outside save_preferences — args pass through."""
    original = {"week": "2026-07-06"}
    result = pin_identity("volunteer", "show_schedule", original, internal_id=42)
    assert result == original
