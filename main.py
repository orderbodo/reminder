import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List
from fastapi import FastAPI, Request, HTTPException, Depends
import httpx
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import SessionLocal, Task, DigestLog, UserSetting, Base, engine

load_dotenv()

# Automatically create tables if they don't exist in Supabase
try:
    Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"Warning: could not auto-create tables: {e}")

app = FastAPI(title="Telegram Persistent Reminder Bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET", "super-secret-key-123")
# User timezone: Defaulting to UTC+7 (Asia/Bangkok). Can be set via USER_TIMEZONE in Render.
TIMEZONE_NAME = os.getenv("USER_TIMEZONE", "Asia/Bangkok")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Auto-register command autocomplete menu on Telegram
@app.on_event("startup")
async def register_bot_commands():
    if not BOT_TOKEN:
        return
    commands = [
        {"command": "list", "description": "📋 View all pending tasks"},
        {"command": "pause", "description": "⏸ Pause reminders (1h, 3h, 4h, custom)"},
        {"command": "resume", "description": "▶️ Resume all reminders now"},
        {"command": "start", "description": "👋 Show instructions and guide"}
    ]
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{TELEGRAM_API}/setMyCommands", json={"commands": commands})
    except Exception as e:
        print(f"Failed to register commands: {e}")

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = ZoneInfo("UTC")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Helper: Send Telegram message
async def send_telegram_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    async with httpx.AsyncClient() as client:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)

# Helper: Answer Callback Query for buttons
async def answer_callback_query(callback_query_id: str, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text
        })

def make_task_markup(task_id: int):
    """Generates rich inline buttons with multiple snooze durations."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Done", "callback_data": f"done_{task_id}"},
                {"text": "🗑 Delete", "callback_data": f"delete_{task_id}"}
            ],
            [
                {"text": "⏰ 15m", "callback_data": f"snooze_{task_id}_15"},
                {"text": "⏰ 30m", "callback_data": f"snooze_{task_id}_30"},
                {"text": "⏰ 1h", "callback_data": f"snooze_{task_id}_60"},
                {"text": "⏰ 3h", "callback_data": f"snooze_{task_id}_180"}
            ],
            [
                {"text": "🌙 Tonight (8 PM)", "callback_data": f"snooze_{task_id}_tonight"},
                {"text": "☀️ Tomorrow (9:30 AM)", "callback_data": f"snooze_{task_id}_tomorrow"}
            ]
        ]
    }

def make_pause_markup():
    """Generates buttons to globally pause reminders."""
    return {
        "inline_keyboard": [
            [
                {"text": "⏸ 1 Hour", "callback_data": "gpause_60"},
                {"text": "⏸ 3 Hours", "callback_data": "gpause_180"},
                {"text": "⏸ 4 Hours", "callback_data": "gpause_240"}
            ],
            [
                {"text": "⏸ 1 Day (24h)", "callback_data": "gpause_1440"},
                {"text": "▶️ Resume Now", "callback_data": "gpause_resume"}
            ]
        ]
    }

def parse_time_str(time_str: str, base_date: datetime) -> Optional[datetime]:
    """Helper to parse time like '14:00', '2pm', '2:30pm', '18:30'."""
    clean = time_str.strip().lower()
    # Check 24-hr format HH:MM
    match_24 = re.match(r"^(\d{1,2}):(\d{2})$", clean)
    if match_24:
        h, m = int(match_24.group(1)), int(match_24.group(2))
        return base_date.replace(hour=h, minute=m, second=0, microsecond=0)

    # Check 12-hr format (e.g., 2pm, 2:30pm, 10am)
    match_12 = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", clean)
    if match_12:
        h = int(match_12.group(1))
        m = int(match_12.group(2) or 0)
        ampm = match_12.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return base_date.replace(hour=h, minute=m, second=0, microsecond=0)

    return None

@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "message": "Telegram Reminder Bot is running!"}

# ----------------- TELEGRAM WEBHOOK -----------------
@app.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    # 1. Handle Inline Button Clicks
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        action_data = cb["data"]

        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(TZ)

        # Global Pause Buttons
        if action_data.startswith("gpause_"):
            val = action_data.replace("gpause_", "")
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if not setting:
                setting = UserSetting(user_id=chat_id)
                db.add(setting)

            if val == "resume":
                setting.paused_until = None
                db.commit()
                await answer_callback_query(cb_id, "Reminders resumed! ▶️")
                await send_telegram_message(chat_id, "▶️ *Reminders resumed!* I will notify you as scheduled.")
            else:
                minutes = int(val)
                until_utc = now_utc + timedelta(minutes=minutes)
                setting.paused_until = until_utc
                db.commit()
                local_until = until_utc.astimezone(TZ).strftime("%I:%M %p")
                await answer_callback_query(cb_id, f"Paused for {minutes//60 if minutes>=60 else minutes}h ⏸")
                await send_telegram_message(
                    chat_id,
                    f"⏸ *All reminders paused* until *{local_until}*.\nUse `/resume` to turn back on anytime."
                )
            return {"ok": True}

        # Task Action Buttons
        parts = action_data.split("_")
        action = parts[0]
        task_id = int(parts[1])

        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            await answer_callback_query(cb_id, "Task not found.")
            return {"ok": True}

        if action == "done":
            task.status = "completed"
            task.completed_at = now_utc
            db.commit()
            await answer_callback_query(cb_id, "Marked as done! 🎉")
            await send_telegram_message(chat_id, f"✅ *Completed:* \"{task.task_text}\"\nGreat work!")

        elif action == "delete":
            task.status = "cancelled"
            db.commit()
            await answer_callback_query(cb_id, "Deleted.")
            await send_telegram_message(chat_id, f"🗑 *Deleted task:* \"{task.task_text}\"")

        elif action == "snooze":
            duration_type = parts[2]
            snooze_label = ""

            if duration_type.isdigit():
                minutes = int(duration_type)
                task.snooze_until = now_utc + timedelta(minutes=minutes)
                snooze_label = f"{minutes} minutes"
            elif duration_type == "tonight":
                # Tonight at 20:00 (8 PM)
                target = now_local.replace(hour=20, minute=0, second=0, microsecond=0)
                if target <= now_local:
                    target += timedelta(days=1)
                task.snooze_until = target.astimezone(timezone.utc)
                snooze_label = "tonight at 8:00 PM"
            elif duration_type == "tomorrow":
                # Tomorrow at 09:30 AM
                target = (now_local + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
                task.snooze_until = target.astimezone(timezone.utc)
                snooze_label = "tomorrow morning at 9:30 AM"

            db.commit()
            await answer_callback_query(cb_id, f"Snoozed until {snooze_label} ⏰")
            await send_telegram_message(chat_id, f"⏳ Snoozed: *\"{task.task_text}\"* until {snooze_label}.")

        return {"ok": True}

    # 2. Handle Text Messages
    if "message" in data and "text" in data["message"]:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(TZ)

        if text.startswith("/start"):
            welcome = (
                "👋 *Welcome to your Persistent Reminder Bot!*\n\n"
                "• Send me any text to add a task (e.g. `Finish presentation slides`)\n"
                "• Type `/list` to view all pending tasks\n"
                "• Daily summaries at: *9:30 AM*, *1:30 PM*, and *6:30 PM*\n"
                "• 🌙 *Quiet Hours:* Reminders automatically pause from *10:00 PM to 9:00 AM*\n"
                "• Type `/pause` or `/resume` to mute/unmute reminders anytime!"
            )
            await send_telegram_message(chat_id, welcome)
            return {"ok": True}

        # Global Pause Commands
        if text.startswith("/resume"):
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if setting:
                setting.paused_until = None
                db.commit()
            await send_telegram_message(chat_id, "▶️ *Reminders resumed!* I'm back on duty.")
            return {"ok": True}

        if text.startswith("/pause"):
            args = text.replace("/pause", "").strip()

            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if not setting:
                setting = UserSetting(user_id=chat_id)
                db.add(setting)

            if not args:
                # Show quick button menu
                await send_telegram_message(
                    chat_id,
                    "⏸ *Pause Reminders*\nChoose how long you'd like to silence all reminders:",
                    reply_markup=make_pause_markup()
                )
                return {"ok": True}

            # Parse custom args:
            # e.g., "/pause 3h", "/pause 4h", "/pause 1d"
            quick_match = re.match(r"^(\d+)\s*(h|hr|hours?|m|mins?|d|days?)$", args, re.IGNORECASE)
            if quick_match:
                amount = int(quick_match.group(1))
                unit = quick_match.group(2).lower()
                if unit.startswith("m"):
                    delta = timedelta(minutes=amount)
                elif unit.startswith("d"):
                    delta = timedelta(days=amount)
                else:
                    delta = timedelta(hours=amount)

                until_utc = now_utc + delta
                setting.paused_until = until_utc
                db.commit()
                local_until = until_utc.astimezone(TZ).strftime("%A, %I:%M %p")
                await send_telegram_message(chat_id, f"⏸ *Reminders paused* until *{local_until}*.\nUse `/resume` to turn back on.")
                return {"ok": True}

            # e.g., "/pause from 14:00 to 18:00" or "/pause from 2pm to 6pm"
            from_to_match = re.match(r"^from\s+(.+?)\s+to\s+(.+)$", args, re.IGNORECASE)
            if from_to_match:
                to_part = from_to_match.group(2).strip()
                parsed_to = parse_time_str(to_part, now_local)
                if parsed_to:
                    if parsed_to <= now_local:
                        parsed_to += timedelta(days=1)
                    setting.paused_until = parsed_to.astimezone(timezone.utc)
                    db.commit()
                    local_until = parsed_to.strftime("%I:%M %p")
                    await send_telegram_message(chat_id, f"⏸ *Reminders paused* until *{local_until}*.\nUse `/resume` to resume earlier.")
                    return {"ok": True}

            # e.g., "/pause to 18:00" or "/pause until 6pm"
            until_match = re.match(r"^(?:until|to)\s+(.+)$", args, re.IGNORECASE)
            if until_match:
                parsed_time = parse_time_str(until_match.group(1).strip(), now_local)
                if parsed_time:
                    if parsed_time <= now_local:
                        parsed_time += timedelta(days=1)
                    setting.paused_until = parsed_time.astimezone(timezone.utc)
                    db.commit()
                    local_until = parsed_time.strftime("%I:%M %p")
                    await send_telegram_message(chat_id, f"⏸ *Reminders paused* until *{local_until}*.")
                    return {"ok": True}

            await send_telegram_message(
                chat_id,
                "⚠️ *Format not recognized.*\n\nTry:\n• `/pause` (opens button menu)\n• `/pause 3h`\n• `/pause 4h`\n• `/pause 1d`\n• `/pause from 2pm to 6pm`\n• `/pause until 5:30pm`"
            )
            return {"ok": True}

        if text.startswith("/list"):
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").all()
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()

            status_header = ""
            if setting and setting.paused_until and setting.paused_until > now_utc:
                local_pause = setting.paused_until.astimezone(TZ).strftime("%I:%M %p")
                status_header = f"⏸ _All reminders are currently paused until {local_pause}_\n\n"

            if not tasks:
                await send_telegram_message(chat_id, f"{status_header}🎉 You have no pending tasks! You're all caught up.")
            else:
                lines = [f"{status_header}📋 *Your Pending Tasks:*\n"]
                for idx, t in enumerate(tasks, 1):
                    status_note = ""
                    if t.snooze_until and t.snooze_until > now_utc:
                        local_snooze = t.snooze_until.astimezone(TZ)
                        status_note = f" _(snoozed until {local_snooze.strftime('%H:%M')})_"
                    lines.append(f"{idx}. *{t.task_text}*{status_note}")
                await send_telegram_message(chat_id, "\n".join(lines))
            return {"ok": True}

        # Any normal message creates a new task
        new_task = Task(user_id=chat_id, task_text=text)
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        markup = make_task_markup(new_task.id)
        await send_telegram_message(
            chat_id,
            f"📝 *Added task:* \"{text}\"\nI will nag you periodically until it's finished!",
            reply_markup=markup
        )

    return {"ok": True}

# ----------------- CRON: NAGGING & DAILY DIGESTS -----------------
@app.get("/cron/nag")
async def cron_nag(secret: str, db: Session = Depends(get_db)):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(TZ)
    today_str = now_local.strftime("%Y-%m-%d")

    # 1. Daily Digest Windows: 09:30 AM, 1:30 PM (13:30), 6:30 PM (18:30)
    digest_slot = None
    greeting = ""

    if (now_local.hour == 9 and now_local.minute >= 30) or (now_local.hour == 10 and now_local.minute < 15):
        digest_slot = f"{today_str}_09:30"
        greeting = "☀️ *Morning Kick-off (9:30 AM)*\nHere is your to-do list for today:"
    elif (now_local.hour == 13 and now_local.minute >= 30) or (now_local.hour == 14 and now_local.minute < 15):
        digest_slot = f"{today_str}_13:30"
        greeting = "⚡ *Midday Check-in (1:30 PM)*\nHow are you progressing with these tasks?"
    elif (now_local.hour == 18 and now_local.minute >= 30) or (now_local.hour == 19 and now_local.minute < 15):
        digest_slot = f"{today_str}_18:30"
        greeting = "🌙 *Evening Wrap-up (6:30 PM)*\nOutstanding items remaining today:"

    if digest_slot:
        pending_users = db.query(Task.user_id).filter(Task.status == "pending").distinct().all()
        for (u_id,) in pending_users:
            already_sent = db.query(DigestLog).filter(
                DigestLog.user_id == u_id,
                DigestLog.slot_key == digest_slot
            ).first()

            if not already_sent:
                user_tasks = db.query(Task).filter(Task.user_id == u_id, Task.status == "pending").all()
                if user_tasks:
                    lines = [greeting, ""]
                    for idx, t in enumerate(user_tasks, 1):
                        lines.append(f"{idx}. *{t.task_text}*")
                    lines.append("\n_Keep knocking them out!_")

                    await send_telegram_message(u_id, "\n".join(lines))

                    log_entry = DigestLog(user_id=u_id, slot_key=digest_slot)
                    db.add(log_entry)
                    db.commit()

    # 2. Quiet Hours Check: 10:00 PM (22:00) to 09:00 AM (09:00)
    # Stop all micro-nagging during quiet hours!
    if now_local.hour >= 22 or now_local.hour < 9:
        return {
            "status": "quiet_hours",
            "message": "Quiet hours active (10:00 PM - 9:00 AM). Nagging silenced.",
            "digest_slot": digest_slot
        }

    # 3. Micro Nagging Loop for individual tasks (outside quiet hours)
    tasks_to_nag = db.query(Task).filter(
        Task.status == "pending",
        (Task.snooze_until == None) | (Task.snooze_until <= now_utc)
    ).all()

    nagged_count = 0
    for task in tasks_to_nag:
        # Check if user has globally paused reminders
        user_setting = db.query(UserSetting).filter(UserSetting.user_id == task.user_id).first()
        if user_setting and user_setting.paused_until and user_setting.paused_until > now_utc:
            # Reminders currently globally paused for this user, skip nagging
            continue

        task.remind_count += 1
        task.last_reminded_at = now_utc
        # Auto-snooze for 1 hour so it doesn't repeatedly ping every 15-minute cron execution
        task.snooze_until = now_utc + timedelta(hours=1)
        db.commit()

        markup = make_task_markup(task.id)
        await send_telegram_message(
            chat_id=task.user_id,
            text=f"🔔 *Reminder (Ping #{task.remind_count})*\nHave you finished: *\"{task.task_text}\"*?",
            reply_markup=markup
        )
        nagged_count += 1

    return {
        "status": "success",
        "digest_slot": digest_slot,
        "nagged_tasks": nagged_count
    }
