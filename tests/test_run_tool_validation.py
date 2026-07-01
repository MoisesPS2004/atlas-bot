"""
Vertical Slice — Hueco D: the process-frontier trust boundary of _run_tool.

_run_tool (bot.py) builds argv for subprocess.run → aquarela_cli.py from args
produced by the LLM. There is no shell (list form, no shell=True), so there is
no shell-injection. The real risk is FLAG injection: a value like "--data" or
"-rf" sitting where the CLI's argparse expects a positional/value can be
mis-parsed as an option. It also lets malformed data (a fake "week", a
non-numeric volunteer_id) cross the process boundary unchecked.

SECURITY CONTRACT agreed during the alignment phase ("Grill Me", Session 7):

  1. Per-field allowlist — every LLM-supplied value is validated by the SEMANTICS
     of its argument NAME (a field's meaning is consistent across all 12 CLI
     tools) BEFORE the argv list is built. Pure functions, no subprocess.
  2. Typed & bounded — week/date/arrival/departure are real ISO YYYY-MM-DD
     dates; volunteer_id/shift_id/stay_id are POSITIVE integers; telegram ids
     are positive numeric strings; module/active_only are closed enums; data is
     valid JSON.
  3. Fail-closed anti-flag — a value beginning with "-" where a datum (not a
     flag) is expected is REJECTED. For the free-text/JSON fields (name, data)
     this is strict: a LEADING "-" is rejected, but INTERIOR hyphens are fine
     ("Jean-Paul" is a valid name).
  4. Frontier enforcement — on any invalid arg, _run_tool returns an
     {"ok": false, "error": "invalid argument: ..."} JSON and NEVER reaches
     subprocess.run. Valid args flow through, sanitized, to argv.
  5. Pass-through — fields _run_tool does not itself pass raw into argv (e.g.
     'confirmed', coerced to the literal "true"/"false" in cmd_map) are left
     untouched by the sanitizer.

These tests are EXPECTED to be RED on first run: the validators and the
_run_tool guard do not exist yet. That RED is the proof the door is missing.
"""
import json

import pytest

# conftest.py stubs .env / openai / telegram before this import.
import bot
from call_grok_core import (
    ValidationError,
    sanitize_arg,
    sanitize_tool_args,
)


# ─── 1. Date fields: week / date / arrival / departure ──────────────────────────

@pytest.mark.parametrize("field", ["week", "date", "arrival", "departure"])
@pytest.mark.parametrize("good", ["2026-07-01", "2025-12-31"])
def test_date_fields_accept_real_iso_dates(field, good):
    assert sanitize_arg(field, good) == good


@pytest.mark.parametrize("field", ["week", "date", "arrival", "departure"])
@pytest.mark.parametrize("bad", [
    "--data",            # flag injection
    "-2026-07-01",       # leading dash
    "2026-7-1",          # not zero-padded / not the agreed shape
    "2026-13-40",        # well-formed shape, impossible calendar date
    "01-07-2026",        # wrong order
    "next monday",       # free text
    "",                  # empty
    "2026-07-01; rm -rf",# trailing junk
])
def test_date_fields_reject_malformed_or_flaglike(field, bad):
    with pytest.raises(ValidationError):
        sanitize_arg(field, bad)


# ─── 2. Positive-integer id fields: volunteer_id / shift_id / stay_id ───────────

@pytest.mark.parametrize("field", ["volunteer_id", "shift_id", "stay_id"])
def test_int_ids_accept_positive_int_or_digit_string(field):
    assert sanitize_arg(field, 42) == "42"
    assert sanitize_arg(field, "42") == "42"


@pytest.mark.parametrize("field", ["volunteer_id", "shift_id", "stay_id"])
@pytest.mark.parametrize("bad", [
    "--foo",   # flag injection
    "-5",      # negative / leading dash
    "0",       # not positive
    0,
    -5,
    "1.5",     # not an integer
    "12a",     # non-digit tail
    "",
    "  7  ",   # padded — reject, keep the validator strict
    True,      # bool is an int subclass but is not a volunteer id
])
def test_int_ids_reject_nonpositive_or_flaglike(field, bad):
    with pytest.raises(ValidationError):
        sanitize_arg(field, bad)


# ─── 3. Telegram-id string fields: telegram_id / admin_telegram_id ─────────────

@pytest.mark.parametrize("field", ["telegram_id", "admin_telegram_id"])
def test_telegram_ids_accept_numeric_strings(field):
    assert sanitize_arg(field, "8632082731") == "8632082731"
    assert sanitize_arg(field, 8632082731) == "8632082731"


@pytest.mark.parametrize("field", ["telegram_id", "admin_telegram_id"])
@pytest.mark.parametrize("bad", ["--foo", "-1", "", "abc", "12 34"])
def test_telegram_ids_reject_flaglike_or_nonnumeric(field, bad):
    with pytest.raises(ValidationError):
        sanitize_arg(field, bad)


# ─── 4. Closed enums: module / active_only ──────────────────────────────────────

@pytest.mark.parametrize("good", ["breakfast", "day_rec", "night_rec"])
def test_module_accepts_known_modules(good):
    assert sanitize_arg("module", good) == good


@pytest.mark.parametrize("bad", ["--module", "-day_rec", "lunch", "", "DAY_REC"])
def test_module_rejects_unknown(bad):
    with pytest.raises(ValidationError):
        sanitize_arg("module", bad)


@pytest.mark.parametrize("good", ["true", "false"])
def test_active_only_accepts_true_false(good):
    assert sanitize_arg("active_only", good) == good


@pytest.mark.parametrize("bad", ["--true", "-false", "yes", "1", ""])
def test_active_only_rejects_other(bad):
    with pytest.raises(ValidationError):
        sanitize_arg("active_only", bad)


# ─── 5. JSON field: data — valid JSON, strict leading-dash guard ────────────────

def test_data_accepts_valid_json_object():
    payload = '{"double_ok": true, "notes": "well-behaved"}'
    assert sanitize_arg("data", payload) == payload


def test_data_allows_interior_hyphens():
    # A hyphen INSIDE a JSON string value must not be mistaken for a flag.
    payload = '{"notes": "arrival day-by-day, dash-friendly"}'
    assert sanitize_arg("data", payload) == payload


@pytest.mark.parametrize("bad", [
    "-5",                       # leading dash — rejected even though valid JSON number
    "--data",                   # flag injection
    "{not valid json",          # malformed
    "",                         # empty
])
def test_data_rejects_leading_dash_or_malformed(bad):
    with pytest.raises(ValidationError):
        sanitize_arg("data", bad)


# ─── 6. Free-text field: name — leading-dash guard, interior hyphens allowed ────

@pytest.mark.parametrize("good", ["Marielle", "Jean-Paul", "Ana María", "O'Brien"])
def test_name_accepts_ordinary_names_including_interior_hyphens(good):
    assert sanitize_arg("name", good) == good


@pytest.mark.parametrize("bad", ["-Marielle", "--name", "-rf", ""])
def test_name_rejects_leading_dash_or_empty(bad):
    with pytest.raises(ValidationError):
        sanitize_arg("name", bad)


# ─── 7. sanitize_tool_args: registry-wide, pass-through of unknown fields ────────

def test_sanitize_tool_args_validates_each_known_field():
    out = sanitize_tool_args({"volunteer_id": 7, "week": "2026-07-01"})
    assert out == {"volunteer_id": "7", "week": "2026-07-01"}


def test_sanitize_tool_args_passes_through_unregistered_fields():
    # 'confirmed' is coerced to a literal in cmd_map and is never passed raw
    # into argv, so the sanitizer must leave it untouched (not reject it).
    out = sanitize_tool_args({"volunteer_id": 7, "confirmed": True})
    assert out["confirmed"] is True
    assert out["volunteer_id"] == "7"


def test_sanitize_tool_args_raises_on_any_invalid_known_field():
    with pytest.raises(ValidationError):
        sanitize_tool_args({"volunteer_id": "--exploit"})


def test_sanitize_tool_args_does_not_mutate_input():
    original = {"volunteer_id": 7}
    sanitize_tool_args(original)
    assert original == {"volunteer_id": 7}


# ─── 8. _run_tool frontier: invalid args never reach subprocess ─────────────────

@pytest.fixture
def no_subprocess(monkeypatch):
    """Any call to subprocess.run inside bot is a contract violation for the
    invalid-input path — blow up loudly if the frontier lets it through."""
    def _boom(*a, **kw):
        raise AssertionError(f"subprocess.run must not be reached: {a!r}")
    monkeypatch.setattr(bot.subprocess, "run", _boom)


def test_run_tool_rejects_flag_injection_before_subprocess(no_subprocess):
    # The canonical attack from the Hueco D diagnosis: a flag smuggled into 'week'.
    out = json.loads(bot._run_tool("generate_draft", {"week": "--data"}))
    assert out["ok"] is False
    assert "invalid argument" in out["error"]


def test_run_tool_rejects_nonpositive_volunteer_id_before_subprocess(no_subprocess):
    out = json.loads(bot._run_tool("deactivate_volunteer", {"volunteer_id": "-1"}))
    assert out["ok"] is False
    assert "invalid argument" in out["error"]


def test_run_tool_passes_sanitized_argv_for_valid_input(monkeypatch):
    """Valid args flow through to subprocess with the sanitized value in argv,
    and no stray flag tokens where a datum is expected."""
    captured = {}

    class _Result:
        stdout = '{"ok": true}'

    def _fake_run(argv, *a, **kw):
        captured["argv"] = argv
        return _Result()

    monkeypatch.setattr(bot.subprocess, "run", _fake_run)

    out = json.loads(bot._run_tool("show_schedule", {"week": "2026-07-01"}))
    assert out["ok"] is True
    assert "2026-07-01" in captured["argv"]
    # argparse contract: the only option-looking token is the known flag name.
    assert [t for t in captured["argv"] if t.startswith("--")] == ["--week"]
