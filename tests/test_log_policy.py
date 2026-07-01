"""
Vertical Slice — Hueco E: secret hygiene in logs.

The bot polls getUpdates through PTB -> httpx. httpx logs every request at INFO
via the "httpx" logger: `HTTP Request: {method} {request.url} "..."`
(httpx/_client.py:1025-1028). For a Telegram call request.url is
`https://api.telegram.org/bot<TOKEN>/getUpdates`, so with the root logger at
INFO (bot.py: logging.basicConfig(level=logging.INFO)) the bot token lands in
journald in plaintext.

This is a SECURITY invariant that is safe-by-CONFIGURATION, not
safe-by-construction: a future basicConfig(DEBUG), a new HTTP dependency, or a
deleted line silently re-opens the leak. So it earns a regression net (unlike
the _check_access case, where the sole call site structurally guaranteed the
invariant — commit 3e9449e).

CONTRACT (alignment phase, Session 7 / Hueco E): a pure policy raises the
third-party HTTP-client loggers that emit request URLs (httpx, httpcore) to at
least WARNING, muting the INFO request line that carries the token, WITHOUT
touching the root logger or our own operational logger (which must stay at INFO
so "Atlas bot started", tool results and denials keep being logged).

Per the agreed test pauta: assert directly on logger state via
getEffectiveLevel() rather than intercepting log strings or simulating network
flows. These tests are EXPECTED to be RED on first run: log_policy does not
exist yet.
"""
import logging

import pytest

from log_policy import apply_secret_safe_logging, secret_safe_log_levels

# The loggers that leak the token at INFO, and the operational loggers that must
# keep flowing. "bot" is logging.getLogger(__name__) inside bot.py (imported as
# the top-level module `bot`).
_LEAKING = ("httpx", "httpcore")
_OPERATIONAL = ("", "bot")  # "" == the root logger


@pytest.fixture(autouse=True)
def restore_logging_levels():
    """Snapshot and restore the explicit level of every logger this slice
    touches, so applying the global policy can't leak state across tests."""
    names = list(_LEAKING) + list(_OPERATIONAL)
    saved = {name: logging.getLogger(name).level for name in names}
    # Establish the production baseline the shell sets before applying policy:
    # root at INFO (what bot.py's basicConfig(level=INFO) yields).
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("bot").setLevel(logging.NOTSET)  # inherits root
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


# ─── 1. Pure policy ─────────────────────────────────────────────────────────────

def test_policy_targets_exactly_the_http_client_loggers_at_warning():
    assert secret_safe_log_levels() == {
        "httpx":    logging.WARNING,
        "httpcore": logging.WARNING,
    }


# ─── 2. Applied policy mutes the leaking loggers ────────────────────────────────

@pytest.mark.parametrize("name", _LEAKING)
def test_leaking_http_loggers_are_muted_to_at_least_warning(name):
    apply_secret_safe_logging()
    assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_the_token_bearing_info_line_would_be_suppressed():
    # httpx emits its request line at INFO; at >= WARNING that record never
    # fires, so the token in request.url is never rendered.
    apply_secret_safe_logging()
    httpx_logger = logging.getLogger("httpx")
    assert not httpx_logger.isEnabledFor(logging.INFO)


# ─── 3. Applied policy leaves operational logging intact ────────────────────────

def test_root_logger_stays_at_info():
    apply_secret_safe_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_our_operational_logger_stays_at_info():
    apply_secret_safe_logging()
    # Our own logger is untouched: it inherits the root INFO level and keeps
    # emitting operational logs.
    assert logging.getLogger("bot").getEffectiveLevel() == logging.INFO


# ─── 4. Robustness ──────────────────────────────────────────────────────────────

def test_apply_is_idempotent():
    apply_secret_safe_logging()
    apply_secret_safe_logging()
    for name in _LEAKING:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_policy_is_pure_and_returns_a_fresh_mapping():
    a = secret_safe_log_levels()
    a["httpx"] = logging.DEBUG  # mutating the result must not poison the source
    assert secret_safe_log_levels()["httpx"] == logging.WARNING
