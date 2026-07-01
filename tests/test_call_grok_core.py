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

B.2 adds the parseo/ensamblado helpers used by the tool-dispatch loop:
  10. parse_tool_call_args is a pure json.loads wrapper — no new error
      handling, a malformed JSON string still raises.
  11. build_assistant_tool_calls_message reproduces the exact shape the
      OpenAI-compatible API expects for the echoed tool_calls.
  12. build_tool_result_message / build_denial_result reproduce the exact
      {"role": "tool", ...} / {"ok": false, "error": ...} shapes the driver
      used to build inline.

B.3 adds plan_post_approve_notifications, the triage that replaces the
generic try/except around the post-approve_draft notification block:
  13. Never raises — a missing telegram_id, missing shifts, or missing/blank
      name is classified as invalid with a reason, not an exception.
  14. Entries are evaluated independently — one invalid entry does not
      affect any other entry's classification or ordering.
  15. A valid entry's "name" is reduced to its first token, matching the
      pre-B.3 driver's `vol.get("name", "").split()[0]`.
  16. Empty input returns {"valid": [], "invalid": []}.

0 I/O, 0 mocks needed — these call the pure functions directly.
"""
import json
from types import SimpleNamespace

import pytest

from call_grok_core import (
    authorize,
    pin_identity,
    parse_tool_call_args,
    build_assistant_tool_calls_message,
    build_tool_result_message,
    build_denial_result,
    plan_post_approve_notifications,
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


# ─── Invariant 10-12: B.2 parseo + ensamblado de mensajes ──────────────────────

def test_parse_tool_call_args_is_a_pure_json_loads_wrapper():
    """SDC #10: parses valid JSON exactly like json.loads."""
    assert parse_tool_call_args('{"week": "2026-07-06"}') == {"week": "2026-07-06"}


def test_parse_tool_call_args_still_raises_on_malformed_json():
    """SDC #10: no new error handling — a malformed string still raises,
    same as the driver's bare json.loads() call before B.2."""
    with pytest.raises(Exception):
        parse_tool_call_args("{not valid json")


def test_build_assistant_tool_calls_message_matches_openai_shape():
    """SDC #11: reproduces the exact shape the API expects for the echo."""
    fc = SimpleNamespace(name="show_schedule", arguments='{"week": "2026-07-06"}')
    tc = SimpleNamespace(id="call_1", function=fc)

    result = build_assistant_tool_calls_message("thinking...", [tc])

    assert result == {
        "role": "assistant",
        "content": "thinking...",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "show_schedule", "arguments": '{"week": "2026-07-06"}'},
        }],
    }


def test_build_assistant_tool_calls_message_defaults_none_content_to_empty_string():
    """SDC #11: msg.content is None when the model only returns tool_calls —
    the driver relied on `msg.content or ""` before B.2."""
    fc = SimpleNamespace(name="show_schedule", arguments="{}")
    tc = SimpleNamespace(id="call_1", function=fc)

    result = build_assistant_tool_calls_message(None, [tc])
    assert result["content"] == ""


def test_build_tool_result_message_matches_shape():
    """SDC #12: reproduces the {"role": "tool", ...} shape used by the driver."""
    assert build_tool_result_message("call_1", '{"ok": true}') == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok": true}',
    }


def test_build_denial_result_matches_shape():
    """SDC #12: reproduces the {"ok": false, "error": ...} shape used for authorize() denials."""
    assert json.loads(build_denial_result("permission denied")) == {
        "ok": False, "error": "permission denied",
    }


# ─── Invariant 13-16: B.3 plan_post_approve_notifications ─────────────────────

def test_plan_classifies_a_fully_valid_entry():
    """SDC #13/#15: a well-formed entry lands in "valid" with name reduced
    to its first token."""
    plan = plan_post_approve_notifications([{
        "telegram_id": "111", "name": "Ana Garcia", "language": "es",
        "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
    }])
    assert plan["invalid"] == []
    assert plan["valid"] == [{
        "telegram_id": "111", "name": "Ana", "language": "es",
        "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
    }]


def test_plan_defaults_language_to_en():
    """A missing "language" key falls back to "en", same as the pre-B.3 driver."""
    plan = plan_post_approve_notifications([{
        "telegram_id": "111", "name": "Ana",
        "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
    }])
    assert plan["valid"][0]["language"] == "en"


@pytest.mark.parametrize("entry,expected_reason", [
    ({"telegram_id": "", "name": "Ana", "shifts": [{"date": "2026-07-06", "type": "breakfast"}]}, "missing telegram_id"),
    ({"name": "Ana", "shifts": [{"date": "2026-07-06", "type": "breakfast"}]}, "missing telegram_id"),
    ({"telegram_id": "111", "name": "Ana", "shifts": []}, "missing shifts"),
    ({"telegram_id": "111", "name": "Ana"}, "missing shifts"),
    ({"telegram_id": "111", "shifts": [{"date": "2026-07-06", "type": "breakfast"}]}, "missing name"),
    ({"telegram_id": "111", "name": "   ", "shifts": [{"date": "2026-07-06", "type": "breakfast"}]}, "missing name"),
])
def test_plan_classifies_each_malformation_without_raising(entry, expected_reason):
    """SDC #13: every malformation that used to crash the whole batch is now
    classified with an explicit reason instead of raising."""
    plan = plan_post_approve_notifications([entry])
    assert plan["valid"] == []
    assert len(plan["invalid"]) == 1
    assert plan["invalid"][0]["reason"] == expected_reason
    assert plan["invalid"][0]["entry"] == entry


def test_plan_isolates_a_malformed_entry_from_a_valid_one_in_the_same_batch():
    """SDC #14: this is the core B.3 fix — a malformed entry (no "name",
    which used to raise IndexError and abort the WHOLE batch) does not
    prevent a valid entry elsewhere in the same list from being planned."""
    malformed = {"telegram_id": "333", "shifts": [{"date": "2026-07-06", "type": "breakfast"}]}
    valid_entry = {
        "telegram_id": "444", "name": "Bob Volunteer", "language": "en",
        "shifts": [{"date": "2026-07-06", "type": "breakfast"}],
    }
    plan = plan_post_approve_notifications([malformed, valid_entry])

    assert len(plan["invalid"]) == 1
    assert plan["invalid"][0]["reason"] == "missing name"
    assert len(plan["valid"]) == 1
    assert plan["valid"][0]["telegram_id"] == "444"
    assert plan["valid"][0]["name"] == "Bob"


def test_plan_of_empty_list_is_empty():
    """SDC #16."""
    assert plan_post_approve_notifications([]) == {"valid": [], "invalid": []}
