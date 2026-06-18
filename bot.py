import os
import json
import logging
import sqlite3
import subprocess
from pathlib import Path

from openai import OpenAI
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Paths and constants ─────────────────────────────────────────────────────
_ENV_PATH   = Path(__file__).parent / ".env"
_SOUL_PATH  = Path("/home/moises/aquarela/atlas_soul.md")
_DB_PATH    = "/home/moises/aquarela/aquarela.db"
_ENGINE_PY  = "/home/moises/aquarela/.venv/bin/python"
_ENGINE_CLI = "/home/moises/aquarela/aquarela_cli.py"
_MAX_HISTORY   = 10   # messages per user kept in memory
_MAX_TOOL_ITER = 5    # max agentic loops before escalating

# ─── Load .env ───────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

ENV                = _load_env()
TOKEN              = ENV["ATLAS_BOT_TOKEN"]
OPENROUTER_API_KEY = ENV["OPENROUTER_API_KEY"]
_MOISES_ID         = int(ENV.get("MOISES_TELEGRAM_ID", "8632082731"))

# ─── OpenAI client (OpenRouter) ──────────────────────────────────────────────
_client = OpenAI(
    api_key=ENV["OPENROUTER_API_KEY"],
    base_url="https://openrouter.ai/api/v1",
)

# ─── SOUL ────────────────────────────────────────────────────────────────────
def _load_soul() -> str:
    return _SOUL_PATH.read_text(encoding="utf-8")

SOUL = _load_soul()

# ─── Tool definitions (OpenAI function-calling format) ───────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_draft",
            "description": "Generate the weekly shift schedule draft. Use the Monday of the target week.",
            "parameters": {
                "type": "object",
                "properties": {
                    "week": {"type": "string", "description": "Monday of the week in YYYY-MM-DD format"}
                },
                "required": ["week"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_schedule",
            "description": "Show the full schedule for a given week.",
            "parameters": {
                "type": "object",
                "properties": {
                    "week": {"type": "string", "description": "Monday of the week in YYYY-MM-DD format"}
                },
                "required": ["week"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_preferences",
            "description": "Save a volunteer's shift preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "volunteer_id": {"type": "integer", "description": "The volunteer's internal ID"},
                    "data": {"type": "string", "description": "JSON string with preference fields: breakfast_liking, reception_morning_liking, reception_day_liking, reception_evening_liking, reception_overnight_liking (each: like/ok/avoid), double_ok (bool), desired_free_days (list of day names), notes (string)"}
                },
                "required": ["volunteer_id", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_training",
            "description": "Record or confirm a volunteer's training completion for a module.",
            "parameters": {
                "type": "object",
                "properties": {
                    "volunteer_id": {"type": "integer", "description": "The volunteer's internal ID"},
                    "module": {"type": "string", "enum": ["breakfast", "day_rec", "night_rec"]},
                    "confirmed": {"type": "boolean", "description": "true = completed, false = pending"}
                },
                "required": ["volunteer_id", "module", "confirmed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_no_show",
            "description": "Record that a volunteer did not show up for their assigned shift.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shift_id": {"type": "integer", "description": "The shift's internal ID"},
                    "volunteer_id": {"type": "integer", "description": "The volunteer's internal ID"}
                },
                "required": ["shift_id", "volunteer_id"],
            },
        },
    },
]

# ─── Engine tool runner ───────────────────────────────────────────────────────
def _run_tool(name: str, args: dict) -> str:
    """Call the engine CLI with the given tool name and args. Returns JSON string."""
    cmd_map = {
        "generate_draft":    ["generate-draft",   "--week",      args.get("week", "")],
        "show_schedule":     ["show-schedule",    "--week",      args.get("week", "")],
        "save_preferences":  ["save-preferences", "--volunteer", str(args.get("volunteer_id", "")),
                              "--data", args.get("data", "{}")],
        "confirm_training":  ["confirm-training", "--volunteer", str(args.get("volunteer_id", "")),
                              "--module", args.get("module", ""),
                              "--confirmed", "true" if args.get("confirmed") else "false"],
        "report_no_show":    ["report-no-show",   "--shift",     str(args.get("shift_id", "")),
                              "--volunteer",      str(args.get("volunteer_id", ""))],
    }
    if name not in cmd_map:
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
    try:
        result = subprocess.run(
            [_ENGINE_PY, _ENGINE_CLI] + cmd_map[name],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip() or json.dumps({"ok": False, "error": "empty output"})
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return json.dumps({"ok": False, "error": str(e)})

# ─── Access control ───────────────────────────────────────────────────────────
def _check_access(telegram_id: int) -> tuple[str, int | str] | None:
    """Returns ("volunteer", volunteer_id) or ("admin", role) or None."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT role FROM admins WHERE telegram_id = ?", (str(telegram_id),)
        ).fetchone()
        if row:
            conn.close()
            return ("admin", row["role"])
        row = conn.execute(
            "SELECT id FROM volunteers WHERE telegram_id = ? AND active = 1",
            (str(telegram_id),)
        ).fetchone()
        conn.close()
        if row:
            return ("volunteer", row["id"])
    except Exception as e:
        logger.error(f"Access check failed: {e}")
    return None

def _load_admin_ids() -> set[int]:
    """All admin telegram IDs from DB, always including Moises as fallback."""
    ids = {_MOISES_ID}
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT telegram_id FROM admins").fetchall()
        conn.close()
        ids.update(int(r["telegram_id"]) for r in rows)
    except Exception as e:
        logger.warning(f"Could not load admin IDs: {e}")
    return ids

# ─── Conversation history ─────────────────────────────────────────────────────
_history: dict[int, list[dict]] = {}

def _add_to_history(user_id: int, role: str, content: str) -> None:
    if user_id not in _history:
        _history[user_id] = []
    _history[user_id].append({"role": role, "content": content})
    if len(_history[user_id]) > _MAX_HISTORY:
        _history[user_id] = _history[user_id][-_MAX_HISTORY:]

# ─── Grok call ────────────────────────────────────────────────────────────────
def _call_grok(user_id: int, user_type: str, internal_id: int | str) -> str:
    """Run the agentic loop. Returns final text reply."""
    system = SOUL + f"\n\n---\nCurrent user: {user_type} (internal_id={internal_id})"
    messages = [{"role": "system", "content": system}] + _history[user_id]

    for iteration in range(_MAX_TOOL_ITER):
        response = _client.chat.completions.create(
            model="x-ai/grok-4.3",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        # No tool call — Grok replied with text
        if not msg.tool_calls:
            return msg.content or "..."

        # Tool calls — run each one and feed results back
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]})
        for tc in msg.tool_calls:
            tool_args = json.loads(tc.function.arguments)
            tool_result = _run_tool(tc.function.name, tool_args)
            logger.info(f"Tool {tc.function.name} result: {tool_result[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result,
            })

    # Reached max iterations
    logger.warning(f"Max tool iterations reached for user {user_id}")
    return "Something complex came up and I couldn't finish. I've flagged it for Moises to take a look."

# ─── Telegram handler ─────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text    = update.message.text or ""

    # Access check
    access = _check_access(user_id)
    if access is None:
        await update.message.reply_text(
            "You're not registered yet. Please ask the Aquarela team for an invite link to get started."
        )
        return

    user_type, internal_id = access

    # Add to history and call Grok
    _add_to_history(user_id, "user", text)
    try:
        reply = _call_grok(user_id, user_type, internal_id)
    except Exception as e:
        logger.error(f"Grok call failed for user {user_id}: {e}")
        reply = "Something went wrong on my end. Please try again in a moment."

    _add_to_history(user_id, "assistant", reply)
    await update.message.reply_text(reply)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Atlas bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
