import os
import json
import logging
import sqlite3
import subprocess
from pathlib import Path

from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

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
    {
        "type": "function",
        "function": {
            "name": "show_volunteer",
            "description": "Look up a volunteer by name or Telegram ID to get their internal volunteer_id. Use this before calling any tool that requires a volunteer_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "telegram_id": {
                        "type": "string",
                        "description": "The volunteer's Telegram ID (optional if name is provided)"
                    },
                    "name": {
                        "type": "string",
                        "description": "The volunteer's name or partial name (optional if telegram_id is provided)"
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_draft",
            "description": "Delete an existing draft for a week so it can be regenerated. Only works on drafts not yet published. Use when a draft needs to be recreated after changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "week": {
                        "type": "string",
                        "description": "Monday of the week in YYYY-MM-DD format"
                    }
                },
                "required": ["week"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_admins",
            "description": "Send a notification message to all admins (Moises, Marielle, Thibaut). Use this when escalating issues, flagging problems, or confirming important actions. Always use this instead of just saying you will notify someone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message to send to all admins"
                    }
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_volunteer",
            "description": "Send a private Telegram message to ONE specific volunteer by their Telegram ID. Use this to send each volunteer their shifts after a schedule is approved, or to reply to a volunteer about their own schedule. Always write the message in the volunteer's own language.",
            "parameters": {
                "type": "object",
                "properties": {
                    "telegram_id": {
                        "type": "string",
                        "description": "The volunteer's Telegram ID (the numeric telegram_id from volunteers_to_notify, NOT their internal volunteer id)"
                    },
                    "message": {
                        "type": "string",
                        "description": "The message to send, already written in the volunteer's language"
                    }
                },
                "required": ["telegram_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_volunteers",
            "description": "List all registered volunteers with their names, arrival/departure dates, and internal IDs. Use when an admin asks who is at the hostel or registered.",
            "parameters": {
                "type": "object",
                "properties": {
                    "active_only": {
                        "type": "string",
                        "enum": ["true", "false"],
                        "description": "true = active volunteers only (default), false = include inactive"
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deactivate_volunteer",
            "description": "Deactivate a volunteer, cancelling all their future shifts. Use when a volunteer leaves early or cancels their stay.",
            "parameters": {
                "type": "object",
                "properties": {
                    "volunteer_id": {
                        "type": "integer",
                        "description": "The volunteer's internal ID"
                    }
                },
                "required": ["volunteer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_draft",
            "description": "Approve the weekly schedule draft and publish it. Returns the list of volunteers and their shifts to notify. Call this only when an admin explicitly approves.",
            "parameters": {
                "type": "object",
                "properties": {
                    "week": {
                        "type": "string",
                        "description": "Monday of the week in YYYY-MM-DD format"
                    },
                    "telegram_id": {
                        "type": "string",
                        "description": "Telegram ID of the approving admin"
                    }
                },
                "required": ["week", "telegram_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_volunteer_dates",
            "description": "Update a volunteer's arrival or departure dates. Cancels future shifts outside the new window. Use for extensions, early departures, or date corrections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stay_id": {
                        "type": "integer",
                        "description": "The stay's internal ID"
                    },
                    "arrival": {
                        "type": "string",
                        "description": "New arrival date YYYY-MM-DD (optional)"
                    },
                    "departure": {
                        "type": "string",
                        "description": "New departure date YYYY-MM-DD (optional)"
                    }
                },
                "required": ["stay_id"],
            },
        },
    },
]

_ADMIN_ONLY_TOOLS = {
    "generate_draft",
    "delete_draft",
    "approve_draft",
    "deactivate_volunteer",
    "update_volunteer_dates",
    "list_volunteers",
    "confirm_training",
    "report_no_show",
}

SHIFT_NAMES = {
    "breakfast":            {"en": "Breakfast (6:00–10:00)",           "es": "Desayuno (6:00–10:00)",            "pt": "Café da manhã (6:00–10:00)",       "fr": "Petit-déjeuner (6:00–10:00)"},
    "breakfast_support":    {"en": "Breakfast Support (8:00–12:00)",   "es": "Apoyo desayuno (8:00–12:00)",      "pt": "Apoio café (8:00–12:00)",          "fr": "Support petit-déj (8:00–12:00)"},
    "reception_morning":    {"en": "Reception AM (6:00–12:00)",        "es": "Recepción mañana (6:00–12:00)",    "pt": "Recepção manhã (6:00–12:00)",      "fr": "Réception matin (6:00–12:00)"},
    "reception_day":        {"en": "Reception PM (12:00–18:00)",       "es": "Recepción tarde (12:00–18:00)",    "pt": "Recepção tarde (12:00–18:00)",     "fr": "Réception après-midi (12:00–18:00)"},
    "reception_evening":    {"en": "Reception Eve (18:00–00:00)",      "es": "Recepción noche (18:00–00:00)",    "pt": "Recepção noite (18:00–00:00)",     "fr": "Réception soir (18:00–00:00)"},
    "reception_overnight":  {"en": "Overnight (00:00–6:00)",           "es": "Turno nocturno (00:00–6:00)",      "pt": "Turno noturno (00:00–6:00)",       "fr": "Nuit (00:00–6:00)"},
}

DAY_NAMES = {
    "en": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],
    "es": ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"],
    "pt": ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"],
    "fr": ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"],
}

MONTH_NAMES = {
    "en": ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
    "es": ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"],
    "pt": ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"],
    "fr": ["jan","fév","mar","avr","mai","jun","juil","aoû","sep","oct","nov","déc"],
}

GREETINGS = {
    "en": "Hi {name}! 🌊 Here are your shifts for the week",
    "es": "¡Hola {name}! 🌊 Aquí están tus turnos para la semana",
    "pt": "Oi {name}! 🌊 Aqui estão seus turnos da semana",
    "fr": "Salut {name}! 🌊 Voici tes shifts pour la semaine",
}

CLOSINGS = {
    "en": "See you at the hostel! 🏄 Any questions, write here.",
    "es": "¡Nos vemos en el hostal! 🏄 Cualquier duda, escríbeme aquí.",
    "pt": "Até no hostel! 🏄 Qualquer dúvida, escreve aqui.",
    "fr": "À bientôt à l'auberge! 🏄 Des questions, écris ici.",
}

def _build_shift_message(name: str, week: str, shifts: list, lang: str) -> str:
    """Build a warm, multilingual shift notification message."""
    lang = lang if lang in DAY_NAMES else "en"
    days  = DAY_NAMES[lang]
    months = MONTH_NAMES[lang]
    greeting = GREETINGS[lang].format(name=name)
    lines = [greeting]
    # Week label
    try:
        from datetime import date as _date, timedelta as _timedelta
        monday = _date.fromisoformat(week)
        sunday = monday + _timedelta(days=6)
        wlabel = f"{monday.day}–{sunday.day} {months[monday.month-1]}"
        lines.append(f"📅 {wlabel}:\n")
    except Exception:
        lines.append("")
    for s in shifts:
        try:
            d = _date.fromisoformat(s["date"])
            day_name = days[d.weekday()]
            shift_label = SHIFT_NAMES.get(s["type"], {}).get(lang, s["type"])
            lines.append(f"• {day_name} {d.day} — {shift_label}")
        except Exception:
            lines.append(f"• {s.get('date','?')} — {s.get('type','?')}")
    lines.append("")
    lines.append(CLOSINGS[lang])
    return "\n".join(lines)

async def _notify_admins_tool(message: str, context, acting_user_id: int = 0) -> str:
    """Send a notification to all admins except the one currently acting. Used as a tool by Grok."""
    admin_ids = _load_admin_ids()
    sent = 0
    for admin_id in admin_ids:
        if admin_id == acting_user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"Atlas notification:\n\n{message}",
            )
            sent += 1
        except Exception as e:
            logger.error(f"Could not notify admin {admin_id}: {e}")
    return json.dumps({"ok": True, "notified": sent})

async def _notify_volunteer_tool(telegram_id: str, message: str, context) -> str:
    """Send a private message to one volunteer by Telegram ID. Telegram tool, not CLI."""
    try:
        await context.bot.send_message(
            chat_id=int(telegram_id),
            text=message,
        )
        return json.dumps({"ok": True, "sent_to": telegram_id})
    except Exception as e:
        logger.error(f"Could not notify volunteer {telegram_id}: {e}")
        return json.dumps({"ok": False, "error": str(e)})

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
        "show_volunteer":    ["show-volunteer",
                              *(["--telegram-id", args.get("telegram_id")] if args.get("telegram_id") else []),
                              *(["--name",        args.get("name")]        if args.get("name")        else [])],
        "delete_draft":      ["delete-draft", "--week", args.get("week", "")],
        "list_volunteers":   ["list-volunteers",
                              *(["--active-only", args.get("active_only", "true")])],
        "deactivate_volunteer": ["deactivate-volunteer",
                                  "--volunteer", str(args.get("volunteer_id", ""))],
        "approve_draft":     ["approve-draft",
                               "--week",        args.get("week", ""),
                               "--telegram-id", args.get("telegram_id", "")],
        "update_volunteer_dates": ["update-volunteer-dates",
                                    "--stay-id",   str(args.get("stay_id", "")),
                                    *(["--arrival",   args.get("arrival")]   if args.get("arrival")   else []),
                                    *(["--departure", args.get("departure")] if args.get("departure") else [])],
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
async def _call_grok(user_id: int, user_type: str, internal_id: int | str, context) -> str:
    """Run the agentic loop. Returns final text reply."""
    system = SOUL + f"\n\n---\nCurrent user: {user_type} (internal_id={internal_id}, telegram_id={user_id})"
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
            # Enforce admin-only tools
            if user_type == "volunteer" and tc.function.name in _ADMIN_ONLY_TOOLS:
                logger.warning(f"Volunteer {user_id} attempted admin tool {tc.function.name} — blocked")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"ok": False, "error": "permission denied: this action is for admins only"}),
                })
                continue

            # notify_volunteer may only be triggered by an admin context (Atlas-only after approval)
            if tc.function.name == "notify_volunteer" and user_type == "volunteer":
                logger.warning(f"Volunteer {user_id} attempted notify_volunteer — blocked")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps({"ok": False, "error": "permission denied"}),
                })
                continue
            if tc.function.name == "notify_admins":
                tool_result = await _notify_admins_tool(
                    tool_args.get("message", ""), context, acting_user_id=user_id
                )
            elif tc.function.name == "notify_volunteer":
                tool_result = await _notify_volunteer_tool(
                    tool_args.get("telegram_id", ""),
                    tool_args.get("message", ""),
                    context,
                )
            else:
                tool_result = _run_tool(tc.function.name, tool_args)
                # Auto-notify volunteers when approve_draft succeeds
                if tc.function.name == "approve_draft":
                    try:
                        result_data = json.loads(tool_result)
                        if result_data.get("ok") and result_data.get("volunteers_to_notify"):
                            notified = 0
                            for vol in result_data["volunteers_to_notify"]:
                                tg_id = vol.get("telegram_id")
                                name  = vol.get("name", "").split()[0]
                                lang  = vol.get("language", "en")
                                week  = result_data.get("week", "")
                                shifts = vol.get("shifts", [])
                                if not tg_id or not shifts:
                                    continue
                                msg = _build_shift_message(name, week, shifts, lang)
                                await _notify_volunteer_tool(tg_id, msg, context)
                                notified += 1
                            logger.info(f"Auto-notified {notified} volunteers after approve_draft")
                    except Exception as e:
                        logger.error(f"Auto-notify after approve_draft failed: {e}")
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
        reply = await _call_grok(user_id, user_type, internal_id, context)
    except Exception as e:
        logger.error(f"Grok call failed for user {user_id}: {e}")
        reply = "Something went wrong on my end. Please try again in a moment."

    _add_to_history(user_id, "assistant", reply)
    await update.message.reply_text(reply)

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start, /help, /status commands with a friendly plain-text response."""
    await update.message.reply_text(
        "Hi! I'm Atlas, the operations assistant for Aquarela do Leme.\n"
        "Just write me a message in plain text — no commands needed.\n"
        "How can I help?"
    )

# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",  handle_command))
    app.add_handler(CommandHandler("help",   handle_command))
    app.add_handler(CommandHandler("status", handle_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Atlas bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
