import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, List
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
# User timezone: Defaulting to UTC+7 (Asia/Bangkok).
TIMEZONE_NAME = os.getenv("USER_TIMEZONE", "Asia/Bangkok")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = ZoneInfo("UTC")

# Auto-register command autocomplete menu on Telegram
@app.on_event("startup")
async def register_bot_commands():
    if not BOT_TOKEN:
        return
    commands = [
        {"command": "list", "description": "📋 View pending tasks"},
        {"command": "pause", "description": "⏸ Pause all reminders or specific task"},
        {"command": "resume", "description": "▶️ Resume all reminders now"},
        {"command": "start", "description": "👋 Show instructions and guide"}
    ]
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{TELEGRAM_API}/setMyCommands", json={"commands": commands})
    except Exception as e:
        print(f"Failed to register commands: {e}")

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
    """Clean button row: Done, Snooze (Auto 1h), Delete."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Done", "callback_data": f"done_{task_id}"},
                {"text": "⏰ Snooze (1h)", "callback_data": f"snooze_{task_id}_60"},
                {"text": "🗑 Delete", "callback_data": f"delete_{task_id}"}
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
    clean = time_str.strip().lower()
    match_24 = re.match(r"^(\d{1,2}):(\d{2})$", clean)
    if match_24:
        h, m = int(match_24.group(1)), int(match_24.group(2))
        return base_date.replace(hour=h, minute=m, second=0, microsecond=0)

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

def parse_scheduled_task(text: str, now_local: datetime) -> Tuple[str, Optional[datetime]]:
    clean = text.strip()

    m_in = re.search(r'\b(?:in)\s+(\d+)\s*(hours?|hrs?|h|mins?|minutes?|m)\b', clean, re.IGNORECASE)
    if m_in:
        amt = int(m_in.group(1))
        unit = m_in.group(2).lower()
        delta = timedelta(minutes=amt) if unit.startswith('m') else timedelta(hours=amt)
        target = now_local + delta
        task_text = re.sub(r'\b(?:in)\s+\d+\s*(?:hours?|hrs?|h|mins?|minutes?|m)\b', '', clean, flags=re.IGNORECASE).strip()
        task_text = re.sub(r'^(?:remind me to|remind me)\s+', '', task_text, flags=re.IGNORECASE).strip()
        return task_text or clean, target

    m_at = re.search(r'\b(?:tomorrow\s+at|tomorrow|at|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2})\b', clean, re.IGNORECASE)
    if m_at:
        raw_match = m_at.group(0).lower()
        time_part = m_at.group(1).lower().strip()
        is_tomorrow = "tomorrow" in raw_match
        target_date = now_local + timedelta(days=1) if is_tomorrow else now_local

        m_hm = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', time_part)
        if m_hm:
            h = int(m_hm.group(1))
            m = int(m_hm.group(2) or 0)
            ampm = m_hm.group(3)
            if ampm == 'pm' and h < 12:
                h += 12
            elif ampm == 'am' and h == 12:
                h = 0
            target = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
            if not is_tomorrow and target <= now_local:
                target += timedelta(days=1)
            task_text = clean[:m_at.start()] + clean[m_at.end():]
            task_text = re.sub(r'^(?:remind me to|remind me)\s+', '', task_text, flags=re.IGNORECASE).strip()
            return task_text or clean, target

    clean_task = re.sub(r'^(?:remind me to|remind me)\s+', '', clean, flags=re.IGNORECASE).strip()
    return clean_task or clean, None

def parse_typed_snooze(text: str, now_local: datetime) -> Optional[datetime]:
    clean = text.strip().lower()
    clean = re.sub(r'^snooze\s+', '', clean).strip()

    m_dur = re.match(r'^(\d+)\s*(m|mins?|minutes?|h|hrs?|hours?)$', clean)
    if m_dur:
        amt = int(m_dur.group(1))
        unit = m_dur.group(2)
        delta = timedelta(minutes=amt) if unit.startswith('m') else timedelta(hours=amt)
        return now_local + delta

    is_tomorrow = "tomorrow" in clean
    clean_time = re.sub(r'\b(?:tomorrow|until|to|at)\b', '', clean).strip()
    m_hm = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', clean_time)
    if m_hm:
        h = int(m_hm.group(1))
        m = int(m_hm.group(2) or 0)
        ampm = m_hm.group(3)
        if ampm == 'pm' and h < 12:
            h += 12
        elif ampm == 'am' and h == 12:
            h = 0
        base_date = now_local + timedelta(days=1) if is_tomorrow else now_local
        target = base_date.replace(hour=h, minute=m, second=0, microsecond=0)
        if not is_tomorrow and target <= now_local:
            target += timedelta(days=1)
        return target

    return None

def match_task_keyword(kw: str, task_text: str) -> bool:
    """Matches task names with flexible singular/plural and word boundary check."""
    kw_clean = kw.lower().strip()
    t_clean = task_text.lower()
    if kw_clean in t_clean:
        return True
    if kw_clean.endswith('s') and kw_clean[:-1] in t_clean:
        return True
    for w in kw_clean.split():
        if len(w) > 2 and (w in t_clean or (w.endswith('s') and w[:-1] in t_clean)):
            return True
    return False

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
                await answer_callback_query(cb_id, f"Paused ⏸")
                await send_telegram_message(
                    chat_id,
                    f"⏸ *All reminders paused* until *{local_until}*.\nUse `/resume` to turn back on anytime."
                )
            return {"ok": True}

        # Task Action Buttons (Operates strictly on this single task!)
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
            task.snooze_until = now_utc + timedelta(hours=1)
            db.commit()
            local_until = task.snooze_until.astimezone(TZ).strftime("%I:%M %p")
            await answer_callback_query(cb_id, "Snoozed 1 task for 1h ⏰")
            await send_telegram_message(
                chat_id,
                f"⏳ Snoozed *\"{task.task_text}\"* for 1 hour (until {local_until}).\n_Other tasks continue as scheduled._"
            )

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
                "• Send tasks: `Finish slides` or `Finish slides at 5pm`\n"
                "• Type `/list` to view your pending tasks\n\n"
                "🎯 *Control tasks by NAME or NUMBER:*\n"
                "• `pause slides 2h` ➔ pauses task with 'slides' for 2 hours\n"
                "• `snooze groceries until 6pm` ➔ pauses groceries\n"
                "• `done slides` ➔ marks slide deck completed\n"
                "• `delete groceries` ➔ deletes task\n\n"
                "⏸ *Pause everything:* `/pause` (or `/pause 3h`)\n"
                "▶️ *Resume everything:* `/resume`\n"
                "🌙 *Quiet Hours:* 10:00 PM to 9:00 AM"
            )
            await send_telegram_message(chat_id, welcome)
            return {"ok": True}

        # Command: /resume (Resume all global reminders)
        if text.startswith("/resume"):
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if setting:
                setting.paused_until = None
                db.commit()
            await send_telegram_message(chat_id, "▶️ *All reminders resumed!* Back on duty.")
            return {"ok": True}

        # ----------------- DONE BY NAME OR NUMBER -----------------
        # E.g. 'done slides', 'done 1', 'completed electrician'
        done_match = re.match(r'^(?:done|finish|completed)\s+(.+)$', text, re.IGNORECASE)
        if done_match:
            target_str = done_match.group(1).strip()
            user_tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()

            matched_tasks = []
            if target_str.isdigit():
                num = int(target_str)
                if 1 <= num <= len(user_tasks):
                    matched_tasks.append(user_tasks[num - 1])
            else:
                matched_tasks = [t for t in user_tasks if match_task_keyword(target_str, t.task_text)]

            if matched_tasks:
                for t in matched_tasks:
                    t.status = "completed"
                    t.completed_at = now_utc
                db.commit()
                names = ", ".join([f"*\"{t.task_text}\"*" for t in matched_tasks])
                await send_telegram_message(chat_id, f"✅ Marked done: {names}! 🎉")
                return {"ok": True}

        # ----------------- DELETE BY NAME OR NUMBER -----------------
        # E.g. 'delete groceries', 'del slides', 'cancel 2'
        del_match = re.match(r'^(?:delete|del|cancel)\s+(.+)$', text, re.IGNORECASE)
        if del_match:
            target_str = del_match.group(1).strip()
            user_tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()

            matched_tasks = []
            if target_str.isdigit():
                num = int(target_str)
                if 1 <= num <= len(user_tasks):
                    matched_tasks.append(user_tasks[num - 1])
            else:
                matched_tasks = [t for t in user_tasks if match_task_keyword(target_str, t.task_text)]

            if matched_tasks:
                for t in matched_tasks:
                    t.status = "cancelled"
                db.commit()
                names = ", ".join([f"*\"{t.task_text}\"*" for t in matched_tasks])
                await send_telegram_message(chat_id, f"🗑 Deleted: {names}.")
                return {"ok": True}

        # ----------------- PAUSE/SNOOZE BY NAME OR NUMBER -----------------
        # E.g. 'pause slides 2h', 'snooze groceries until 5pm', 'pause 1 3h'
        pause_named_match = re.match(r'^(?:pause|snooze)\s+(.+)$', text, re.IGNORECASE)
        if pause_named_match:
            rest = pause_named_match.group(1).strip()
            user_tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()

            # Separate task identifier from timing at the end
            timing_regexes = [
                r'\b(\d+\s*(?:h|hr|hours?|m|mins?|minutes?|d|days?))\b$',
                r'\b(until\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\bto\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b$',
                r'\b(at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b$',
                r'\b(tomorrow(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\b$',
                r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b$'
            ]

            extracted_timing = None
            task_kw = rest

            for r in timing_regexes:
                m_t = re.search(r, rest, re.IGNORECASE)
                if m_t:
                    extracted_timing = m_t.group(1).strip()
                    task_kw = rest[:m_t.start()].strip()
                    break

            if extracted_timing and task_kw:
                parsed_target = parse_typed_snooze(extracted_timing, now_local)
                if parsed_target:
                    target_utc = parsed_target.astimezone(timezone.utc)
                    local_display = parsed_target.strftime("%I:%M %p")

                    matched_tasks = []
                    if task_kw.isdigit():
                        num = int(task_kw)
                        if 1 <= num <= len(user_tasks):
                            matched_tasks.append(user_tasks[num - 1])
                    else:
                        matched_tasks = [t for t in user_tasks if match_task_keyword(task_kw, t.task_text)]

                    if matched_tasks:
                        for t in matched_tasks:
                            t.snooze_until = target_utc
                        db.commit()
                        names = ", ".join([f"*\"{t.task_text}\"*" for t in matched_tasks])
                        await send_telegram_message(
                            chat_id,
                            f"⏳ Snoozed {names} until *{local_display}*.\n_Other tasks remain active!_"
                        )
                        return {"ok": True}

        # ----------------- GLOBAL PAUSE COMMANDS -----------------
        if text.startswith("/pause"):
            args = text.replace("/pause", "").strip()
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if not setting:
                setting = UserSetting(user_id=chat_id)
                db.add(setting)

            if not args:
                await send_telegram_message(
                    chat_id,
                    "⏸ *Pause Reminders*\n\n• Tap below to pause **all** reminders\n• Or type `pause slides 2h` to pause a specific task:",
                    reply_markup=make_pause_markup()
                )
                return {"ok": True}

            quick_match = re.match(r"^(\d+)\s*(h|hr|hours?|m|mins?|d|days?)$", args, re.IGNORECASE)
            if quick_match:
                amount = int(quick_match.group(1))
                unit = quick_match.group(2).lower()
                delta = timedelta(minutes=amount) if unit.startswith("m") else (timedelta(days=amount) if unit.startswith("d") else timedelta(hours=amount))
                until_utc = now_utc + delta
                setting.paused_until = until_utc
                db.commit()
                local_until = until_utc.astimezone(TZ).strftime("%A, %I:%M %p")
                await send_telegram_message(chat_id, f"⏸ *All reminders paused* until *{local_until}*.\nUse `/resume` to turn back on.")
                return {"ok": True}

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
                    await send_telegram_message(chat_id, f"⏸ *All reminders paused* until *{local_until}*.\nUse `/resume` to resume earlier.")
                    return {"ok": True}

            until_match = re.match(r"^(?:until|to)\s+(.+)$", args, re.IGNORECASE)
            if until_match:
                parsed_time = parse_time_str(until_match.group(1).strip(), now_local)
                if parsed_time:
                    if parsed_time <= now_local:
                        parsed_time += timedelta(days=1)
                    setting.paused_until = parsed_time.astimezone(timezone.utc)
                    db.commit()
                    local_until = parsed_time.strftime("%I:%M %p")
                    await send_telegram_message(chat_id, f"⏸ *All reminders paused* until *{local_until}*.")
                    return {"ok": True}

            await send_telegram_message(
                chat_id,
                "⚠️ *Format not recognized.*\n\nTry:\n• `pause slides 2h` (pauses specific task by name)\n• `/pause` (button menu)\n• `/pause 3h`\n• `/pause until 5pm`"
            )
            return {"ok": True}

        if text.startswith("/list"):
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()

            status_header = ""
            if setting and setting.paused_until and setting.paused_until > now_utc:
                local_pause = setting.paused_until.astimezone(TZ).strftime("%I:%M %p")
                status_header = f"⏸ _All reminders are currently globally paused until {local_pause}_\n\n"

            if not tasks:
                await send_telegram_message(chat_id, f"{status_header}🎉 You have no pending tasks! You're all caught up.")
            else:
                lines = [f"{status_header}📋 *Your Pending Tasks:*\n"]
                for idx, t in enumerate(tasks, 1):
                    status_note = ""
                    if t.snooze_until and t.snooze_until > now_utc:
                        local_snooze = t.snooze_until.astimezone(TZ)
                        status_note = f" _(snoozed until {local_snooze.strftime('%I:%M %p')})_"
                    lines.append(f"{idx}. *{t.task_text}*{status_note}")

                lines.append("\n💡 _Quick Controls (by name or number):_")
                lines.append("• `pause slides 2h` or `pause 1 2h`")
                lines.append("• `snooze groceries until 5pm`")
                lines.append("• `done slides` or `done 1`")
                await send_telegram_message(chat_id, "\n".join(lines))
            return {"ok": True}

        # Custom typed snooze for the latest reminded task (e.g. '30m', 'until 5pm')
        typed_snooze = parse_typed_snooze(text, now_local)
        if typed_snooze:
            recent_task = db.query(Task).filter(
                Task.user_id == chat_id,
                Task.status == "pending"
            ).order_by(Task.last_reminded_at.desc().nullslast(), Task.id.desc()).first()

            if recent_task:
                recent_task.snooze_until = typed_snooze.astimezone(timezone.utc)
                db.commit()
                local_until = typed_snooze.strftime("%I:%M %p")
                await send_telegram_message(
                    chat_id,
                    f"⏳ Snoozed: *\"{recent_task.task_text}\"* until *{local_until}*.\n_Other tasks will remind as scheduled!_"
                )
                return {"ok": True}

        # Otherwise: Create new task (with optional scheduling)
        task_desc, scheduled_time = parse_scheduled_task(text, now_local)
        snooze_target = scheduled_time.astimezone(timezone.utc) if scheduled_time else None

        new_task = Task(user_id=chat_id, task_text=task_desc, snooze_until=snooze_target)
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        markup = make_task_markup(new_task.id)
        if scheduled_time:
            time_display = scheduled_time.strftime("%I:%M %p")
            day_display = "tomorrow" if scheduled_time.date() > now_local.date() else "today"
            await send_telegram_message(
                chat_id,
                f"📝 *Scheduled task:* \"{task_desc}\"\n⏰ I will stay quiet and start nagging you {day_display} at *{time_display}*!",
                reply_markup=markup
            )
        else:
            await send_telegram_message(
                chat_id,
                f"📝 *Added task:* \"{task_desc}\"\nI will nag you periodically until it's finished!",
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
                user_tasks = db.query(Task).filter(Task.user_id == u_id, Task.status == "pending").order_by(Task.id.asc()).all()
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
    if now_local.hour >= 22 or now_local.hour < 9:
        return {
            "status": "quiet_hours",
            "message": "Quiet hours active (10:00 PM - 9:00 AM). Nagging silenced.",
            "digest_slot": digest_slot
        }

    # 3. Micro Nagging Loop for individual tasks
    tasks_to_nag = db.query(Task).filter(
        Task.status == "pending",
        (Task.snooze_until == None) | (Task.snooze_until <= now_utc)
    ).all()

    nagged_count = 0
    for task in tasks_to_nag:
        user_setting = db.query(UserSetting).filter(UserSetting.user_id == task.user_id).first()
        if user_setting and user_setting.paused_until and user_setting.paused_until > now_utc:
            continue

        task.remind_count += 1
        task.last_reminded_at = now_utc
        # Auto-snooze for 1 hour by default: if user ignores or does not answer in 2 mins, it remains auto-snoozed for 1h
        task.snooze_until = now_utc + timedelta(hours=1)
        db.commit()

        markup = make_task_markup(task.id)
        await send_telegram_message(
            chat_id=task.user_id,
            text=(
                f"🔔 *Reminder (Ping #{task.remind_count})*\n"
                f"Have you finished: *\"{task.task_text}\"*?\n\n"
                f"_(Auto-snoozes for 1h if no reply. Reply with custom time like `30m`, `2h`, `until 5pm` to customize)_"
            ),
            reply_markup=markup
        )
        nagged_count += 1

    return {
        "status": "success",
        "digest_slot": digest_slot,
        "nagged_tasks": nagged_count
    }
