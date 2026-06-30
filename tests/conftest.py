"""
Stubs for atlas-bot module-level side effects so test files can import bot
without a real .env, a live OpenRouter key, or a running Telegram bot.
"""
import sys
import types
from unittest.mock import MagicMock, patch

# ── 1. Fake .env so _load_env() succeeds ────────────────────────────────────
import os
os.environ.setdefault("ATLAS_BOT_TOKEN",        "fake-token")
os.environ.setdefault("OPENROUTER_API_KEY",     "fake-key")
os.environ.setdefault("MOISES_TELEGRAM_ID",     "8632082731")

# ── 2. Stub openai so OpenAI(...) doesn't reach the network ─────────────────
_fake_openai = types.ModuleType("openai")
_fake_openai.OpenAI = MagicMock(return_value=MagicMock())
sys.modules.setdefault("openai", _fake_openai)

# ── 3. Stub python-telegram-bot ─────────────────────────────────────────────
for mod in ("telegram", "telegram.ext"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

tg = sys.modules["telegram"]
tg.Update       = MagicMock()
tg.BotCommand   = MagicMock()

tg_ext = sys.modules["telegram.ext"]
tg_ext.Application      = MagicMock()
tg_ext.CommandHandler   = MagicMock()
tg_ext.MessageHandler   = MagicMock()
tg_ext.ContextTypes     = MagicMock()
tg_ext.filters          = MagicMock()

# ── 4. Stub training_callbacks (local sibling module) ───────────────────────
_tc = types.ModuleType("training_callbacks")
_tc.handle_training_callback = MagicMock()
sys.modules.setdefault("training_callbacks", _tc)

# ── 5. Patch _load_env so bot doesn't open the real file at import time ─────
import builtins
_real_open = builtins.open

def _fake_open(path, *a, **kw):
    p = str(path)
    if p.endswith(".env"):
        from io import StringIO
        fake = StringIO(
            "ATLAS_BOT_TOKEN=fake-token\n"
            "OPENROUTER_API_KEY=fake-key\n"
            "MOISES_TELEGRAM_ID=8632082731\n"
        )
        fake.name = p
        return fake
    return _real_open(path, *a, **kw)

builtins.open = _fake_open
