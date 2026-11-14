import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, List
from fastapi import FastAPI, Request, HTTPException, Depends
import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import SessionLocal, Task, DigestLog, UserSetting, Base, engine
import ai_parser

# Optional: comma-separated Telegram user IDs allowed to use the AI (protects your API quota).
# Leave empty to allow everyone.
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

load_dotenv()

# Automatically create/upgrade tables if they don't exist in Supabase
try:
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS interval_mins INT DEFAULT 60;"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ NULL;"))
        conn.commit()
except Exception as e:
    print(f"Database init/migration note: {e}")

app = FastAPI(title="Telegram Persistent Reminder Bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET", "super-secret-key-123")
# User timezone: Defaulting to UTC+7 (Asia/Bangkok / Vietnam).
TIMEZONE_NAME = os.getenv("USER_TIMEZONE", "Asia/Bangkok")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TZ = ZoneInfo("UTC")

@app.on_event("startup")
async def register_bot_commands():
    if not BOT_TOKEN:
        return
    commands = [
        {"command": "list", "description": "📋 View all pending tasks"},
        {"command": "pause", "description": "⏸ Pause all reminders (1h, 3h, 4h...)"},
        {"command": "resume", "description": "▶️ Resume all reminders now"},
        {"command": "start", "description": "👋 Instructions and format guide"}
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
async def send_telegram_message(chat_id: int, message_text: str, reply_markup: Optional[dict] = None):
    async with httpx.AsyncClient() as client:
        payload = {"chat_id": chat_id, "text": message_text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)

# Helper: Answer Callback Query for buttons
async def answer_callback_query(callback_query_id: str, feedback: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": feedback
        })

def format_interval_label(minutes: int) -> str:
    """e.g. 30 -> '30m', 60 -> '1h', 120 -> '2h', 90 -> '1h30m'."""
    if minutes < 60:
        return f"{minutes}m"
    h = minutes // 60
    m = minutes % 60
    return f"{h}h" if m == 0 else f"{h}h{m}m"

def make_task_markup(task_id: int, interval_mins: int = 60):
    lbl = format_interval_label(interval_mins)
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Done / Xong", "callback_data": f"done_{task_id}"},
                {"text": f"⏰ Snooze ({lbl})", "callback_data": f"snooze_{task_id}_{interval_mins}"},
                {"text": "🗑 Delete / Xoá", "callback_data": f"delete_{task_id}"}
            ]
        ]
    }

def make_pause_markup():
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

# --- BILINGUAL PARSING ENGINE (English & Vietnamese) ---
def parse_time_token(time_str: str, base_date: datetime) -> Optional[datetime]:
    """Parses '11h', '11h30', '11:30', '15h', '3pm', '9:30am'."""
    clean = time_str.strip().lower()
    # 24h format: 11h, 11h30, 11:30, 15h
    m_24 = re.match(r"^(\d{1,2})(?:h|:)(\d{2})?$", clean)
    if m_24:
        h = int(m_24.group(1))
        m = int(m_24.group(2) or 0)
        return base_date.replace(hour=h, minute=m, second=0, microsecond=0)

    # 12h format: 3pm, 9:30am
    m_12 = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", clean)
    if m_12:
        h = int(m_12.group(1))
        m = int(m_12.group(2) or 0)
        ampm = m_12.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        return base_date.replace(hour=h, minute=m, second=0, microsecond=0)

    return None

def parse_interval_token(int_str: str) -> int:
    """Extracts duration in minutes from e.g. 'mỗi 30p', 'every 30m', 'mỗi 2h', 'every 45 mins'."""
    clean = int_str.strip().lower()
    m = re.search(r"(\d+)\s*(p|ph|phút|mins?|minutes?|m|h|giờ|tiếng|hours?)", clean)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit.startswith(("p", "ph", "m")):
            return val
        else:
            return val * 60
    return 60

def parse_structured_task(input_text: str, now_local: datetime) -> Tuple[str, Optional[datetime], Optional[datetime], int]:
    """
    Parses structured syntax in English or Vietnamese:
    'Finish slides - from 11:00 to 15:00 - snooze every 30m'
    'Làm slide - từ 11h đến 15h - snooze mỗi 30p'
    'Team meeting - at 14:00'
    'Drink water - every 45m'
    """
    parts = [p.strip() for p in re.split(r"\s*[-|]\s*", input_text) if p.strip()]
    task_name = parts[0]
    start_time = None
    end_time = None
    interval_mins = 60  # Default: 1 hour

    for part in parts[1:]:
        p_lower = part.lower()

        # 1. Custom snooze interval (e.g. 'snooze every 30m', 'mỗi 30p', 'every 45m')
        if any(k in p_lower for k in ["snooze", "mỗi", "every", "lặp", "repeat", "each"]):
            interval_mins = parse_interval_token(part)
            continue

        # 2. Time range: 'from 11:00 to 15:00' / 'từ 11h đến 15h'
        m_range = re.search(r"(?:from|từ)\s+([^\s]+)\s+(?:to|đến|tới)\s+([^\s]+)", p_lower)
        if m_range:
            t1 = parse_time_token(m_range.group(1), now_local)
            t2 = parse_time_token(m_range.group(2), now_local)
            if t1 and t2:
                if t1 <= now_local and t2 <= now_local:
                    t1 += timedelta(days=1)
                    t2 += timedelta(days=1)
                elif t2 < t1:
                    t2 += timedelta(days=1)
                start_time = t1
                end_time = t2
                continue

        # 3. Single start time: 'at 14:00', 'lúc 14h', 'từ 11h', 'from 11:00'
        m_single = re.search(r"(?:at|lúc|from|từ)\s+([^\s]+)", p_lower)
        if m_single:
            t1 = parse_time_token(m_single.group(1), now_local)
            if t1:
                if t1 <= now_local:
                    t1 += timedelta(days=1)
                start_time = t1
                continue

    # Quick inline without dash e.g. 'Finish slides at 3pm' or 'Làm slide lúc 15h'
    if not start_time:
        m_inline = re.search(r"\b(?:at|lúc)\s+(\d{1,2}(?:h|:)?(?:\d{2})?\s*(?:am|pm)?)\b", task_name, re.IGNORECASE)
        if m_inline:
            t = parse_time_token(m_inline.group(1), now_local)
            if t:
                if t <= now_local:
                    t += timedelta(days=1)
                start_time = t
                task_name = task_name[:m_inline.start()] + task_name[m_inline.end():]
                task_name = task_name.strip()

    return task_name, start_time, end_time, interval_mins

def parse_typed_snooze(text: str, now_local: datetime) -> Optional[datetime]:
    """Parses custom typed snoozing: '30m', '30p', '45 mins', '2h', '2 tiếng', 'until 5pm', 'đến 17h'."""
    clean = text.strip().lower()
    clean = re.sub(r"^(?:snooze|pause|hoãn|hoan)\s+", "", clean).strip()

    # Relative duration: 15p, 30m, 2h, 3 hours, 2 tiếng
    m_dur = re.match(r"^(\d+)\s*(p|ph|phút|mins?|minutes?|m|h|giờ|tiếng|hours?)$", clean)
    if m_dur:
        amt = int(m_dur.group(1))
        unit = m_dur.group(2)
        delta = timedelta(minutes=amt) if unit.startswith(("p", "ph", "m")) else timedelta(hours=amt)
        return now_local + delta

    # Absolute time: until 5pm, lúc 17h, 18:30, đến 17h
    is_tomorrow = any(w in clean for w in ["tomorrow", "ngày mai", "mai"])
    clean_time = re.sub(r"\b(?:tomorrow|ngày mai|mai|until|to|at|lúc|đến|toi)\b", "", clean).strip()
    target_time = parse_time_token(clean_time, now_local)
    if target_time:
        if is_tomorrow or target_time <= now_local:
            target_time += timedelta(days=1)
        return target_time

    return None

def match_task_keyword(kw: str, task_text: str) -> bool:
    kw_clean = kw.lower().strip()
    t_clean = task_text.lower()
    if kw_clean in t_clean:
        return True
    if kw_clean.endswith("s") and kw_clean[:-1] in t_clean:
        return True
    for w in kw_clean.split():
        if len(w) > 2 and (w in t_clean or (w.endswith("s") and w[:-1] in t_clean)):
            return True
    return False

@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "message": "Telegram Reminder Bot is running!"}

# ----------------- AI ACTION EXECUTOR -----------------
def _clean(s: str) -> str:
    """Strip Markdown control chars so AI-written text can't break Telegram formatting."""
    return re.sub(r"[*_`\[\]]", "", str(s or "")).strip()

def _parse_iso(value, fallback_tz=TZ) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=fallback_tz)
        return dt
    except Exception:
        return None

def apply_ai_actions(db: Session, chat_id: int, actions: List[dict]) -> List[str]:
    """Executes AI-decided actions against the DB. Returns reply lines for Telegram."""
    replies: List[str] = []
    now_utc = datetime.now(timezone.utc)

    for act in actions:
        kind = act.get("type")

        if kind == "add_task":
            name = _clean(act.get("task"))
            if not name:
                continue
            start = _parse_iso(act.get("start_iso"))
            end = _parse_iso(act.get("end_iso"))
            interval = act.get("interval_mins")
            interval = int(interval) if isinstance(interval, (int, float)) and interval > 0 else 60

            task = Task(
                user_id=chat_id,
                task_text=name,
                snooze_until=start.astimezone(timezone.utc) if start and start > datetime.now(TZ) else None,
                end_time=end.astimezone(timezone.utc) if end else None,
                interval_mins=interval,
            )
            db.add(task)
            db.commit()

            line = f"📝 *Added:* {name}"
            if start and start > datetime.now(TZ):
                line += f"\n   ⏰ starts {start.astimezone(TZ).strftime('%a %H:%M')}"
            if end:
                line += f"\n   🏁 until {end.astimezone(TZ).strftime('%a %H:%M')}"
            line += f"\n   🔄 every {format_interval_label(interval)}"
            replies.append(line)

        elif kind in ("done", "delete", "snooze", "set_interval"):
            task = db.query(Task).filter(
                Task.id == act.get("task_id"), Task.user_id == chat_id, Task.status == "pending"
            ).first()
            if not task:
                replies.append("⚠️ I couldn't find that task. Type /list to see your tasks.")
                continue

            if kind == "done":
                task.status, task.completed_at = "completed", now_utc
                replies.append(f"✅ *Done:* {_clean(task.task_text)} 🎉")
            elif kind == "delete":
                task.status = "cancelled"
                replies.append(f"🗑 *Deleted:* {_clean(task.task_text)}")
            elif kind == "snooze":
                until = _parse_iso(act.get("until_iso"))
                if not until:
                    replies.append("⚠️ I didn't catch the snooze time.")
                    continue
                task.snooze_until = until.astimezone(timezone.utc)
                replies.append(f"⏳ *Snoozed:* {_clean(task.task_text)} until {until.astimezone(TZ).strftime('%a %H:%M')}")
            elif kind == "set_interval":
                mins = int(act.get("interval_mins") or 60)
                task.interval_mins = mins
                replies.append(f"🔄 *{_clean(task.task_text)}* will now nag every {format_interval_label(mins)}")
            db.commit()

        elif kind == "pause_all":
            until = _parse_iso(act.get("until_iso"))
            if not until:
                replies.append("⚠️ I didn't catch how long to pause.")
                continue
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if not setting:
                setting = UserSetting(user_id=chat_id)
                db.add(setting)
            setting.paused_until = until.astimezone(timezone.utc)
            db.commit()
            replies.append(f"⏸ *All reminders paused* until {until.astimezone(TZ).strftime('%a %H:%M')}")

        elif kind == "resume_all":
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if setting:
                setting.paused_until = None
                db.commit()
            replies.append("▶️ *Reminders resumed!*")

        elif kind == "list":
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
            if not tasks:
                replies.append("🎉 No pending tasks!")
            else:
                lines = ["📋 *Pending tasks:*"]
                for i, t in enumerate(tasks, 1):
                    note = ""
                    if t.snooze_until and t.snooze_until > now_utc:
                        note = f" _(quiet until {t.snooze_until.astimezone(TZ).strftime('%a %H:%M')})_"
                    lines.append(f"{i}. {_clean(t.task_text)}{note}")
                replies.append("\n".join(lines))

        elif kind == "chat":
            replies.append(_clean(act.get("reply")) or "🤔 Could you rephrase that?")

    return replies

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
                await send_telegram_message(chat_id, "▶️ *Reminders resumed!* Bot will notify as scheduled.")
            else:
                minutes = int(val)
                until_utc = now_utc + timedelta(minutes=minutes)
                setting.paused_until = until_utc
                db.commit()
                local_until = until_utc.astimezone(TZ).strftime("%I:%M %p (%H:%M)")
                await answer_callback_query(cb_id, f"Paused ⏸")
                await send_telegram_message(
                    chat_id,
                    f"⏸ *All reminders paused* until *{local_until}*.\nType `/resume` to turn back on anytime."
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
            await answer_callback_query(cb_id, "Marked done! 🎉")
            await send_telegram_message(chat_id, f"✅ *Completed:* \"{task.task_text}\"\nGreat job! 🎉")

        elif action == "delete":
            task.status = "cancelled"
            db.commit()
            await answer_callback_query(cb_id, "Deleted.")
            await send_telegram_message(chat_id, f"🗑 *Deleted task:* \"{task.task_text}\"")

        elif action == "snooze":
            # Snooze for the task's custom chosen period (or passed parameter)
            interval = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else (task.interval_mins or 60)
            task.snooze_until = now_utc + timedelta(minutes=interval)
            db.commit()
            local_until = task.snooze_until.astimezone(TZ).strftime("%I:%M %p (%H:%M)")
            lbl = format_interval_label(interval)
            await answer_callback_query(cb_id, f"Snoozed for {lbl} ⏰")
            await send_telegram_message(
                chat_id,
                f"⏳ Snoozed *\"{task.task_text}\"* for {lbl} (until {local_until})."
            )

        return {"ok": True}

    # 2. Handle Text Messages
    if "message" in data and "text" in data["message"]:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text_content = msg["text"].strip()
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(TZ)

        # Concise, clear English guide
        if text_content.startswith("/start"):
            welcome = (
                "👋 *Persistent Reminder Bot*\n"
                "_I will nag you repeatedly until your tasks are finished!_\n\n"
                "📝 *Add a Task (Format: `Task - Time - Repeat`):*\n"
                "• `Làm slide - từ 11h đến 15h - mỗi 30p`\n"
                "• `Team meeting - at 14:00`\n"
                "• `Drink water - every 45m`\n"
                "• `Read book` _(starts now, repeats every 1h)_\n\n"
                "⏰ *How Reminders Work:*\n"
                "• **No reply in 2 mins?** Auto-snoozes for 1 hour.\n"
                "• Tap `[✅ Done]` when finished, or `[⏰ Snooze]` to delay.\n"
                "• Quick text: `done slide` • `pause slide 2h`\n\n"
                "📋 *Commands:*\n"
                "`/list` • `/pause` • `/resume`\n"
                "🌙 *Quiet Hours:* 10:00 PM – 9:00 AM"
            )
            await send_telegram_message(chat_id, welcome)
            return {"ok": True}

        # Command: /resume
        if text_content.startswith("/resume"):
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if setting:
                setting.paused_until = None
                db.commit()
            await send_telegram_message(chat_id, "▶️ *Reminders resumed!* Back on duty.")
            return {"ok": True}

        # ----------------- AI BRAIN (understands free-form EN / VI) -----------------
        # Slash commands (/list, /pause...) stay rule-based. Everything else goes to the AI.
        # If the AI is unavailable or fails, we silently fall through to the rule-based parser below.
        ai_allowed = not ALLOWED_USER_IDS or chat_id in ALLOWED_USER_IDS
        if ai_allowed and ai_parser.GEMINI_API_KEY and not text_content.startswith("/"):
            pending = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
            ai_actions = await ai_parser.interpret(
                text_content, now_local, [{"id": t.id, "text": t.task_text} for t in pending]
            )
            if ai_actions:
                replies = apply_ai_actions(db, chat_id, ai_actions)
                if replies:
                    await send_telegram_message(chat_id, "\n\n".join(replies))
                    return {"ok": True}

        # ----------------- DONE BY NAME OR NUMBER (Bilingual) -----------------
        # E.g. 'done slides', 'xong slide', 'done 1', 'completed report'
        done_match = re.match(r"^(?:done|finish|completed|xong|hoan thanh|hoàn thành)\s+(.+)$", text_content, re.IGNORECASE)
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

        # ----------------- DELETE BY NAME OR NUMBER (Bilingual) -----------------
        # E.g. 'delete groceries', 'xoá slide', 'del 1'
        del_match = re.match(r"^(?:delete|del|cancel|xoá|xoa|huỷ|huy)\s+(.+)$", text_content, re.IGNORECASE)
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

        # ----------------- PAUSE/SNOOZE SPECIFIC TASK BY NAME OR NUMBER (Bilingual) -----------------
        # E.g. 'pause slides 2h', 'hoãn slide 30p', 'snooze groceries until 5pm'
        pause_named_match = re.match(r"^(?:pause|snooze|hoãn|hoan)\s+(.+)$", text_content, re.IGNORECASE)
        if pause_named_match:
            rest = pause_named_match.group(1).strip()
            user_tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()

            timing_regexes = [
                r"\b(\d+\s*(?:p|ph|phút|mins?|minutes?|m|h|giờ|tiếng|hours?))\b$",
                r"\b((?:until|to|đến|tới)\s+\d{1,2}(?:h|:)?(?:\d{2})?\s*(?:am|pm)?)\b$",
                r"\b((?:at|lúc)\s+\d{1,2}(?:h|:)?(?:\d{2})?\s*(?:am|pm)?)\b$",
                r"\b(tomorrow|ngày mai|mai)\b$",
                r"\b(\d{1,2}(?:h|:)(?:\d{2})?)\b$"
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
                    local_display = parsed_target.strftime("%I:%M %p (%H:%M)")

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
        if text_content.startswith("/pause"):
            args = text_content.replace("/pause", "").strip()
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if not setting:
                setting = UserSetting(user_id=chat_id)
                db.add(setting)

            if not args:
                await send_telegram_message(
                    chat_id,
                    "⏸ *PAUSE ALL REMINDERS*\n\n• Tap below to pause **all** reminders\n• Or type `pause slides 2h` to pause a specific task:",
                    reply_markup=make_pause_markup()
                )
                return {"ok": True}

            quick_match = re.match(r"^(\d+)\s*(h|hr|hours?|giờ|tiếng|m|mins?|p|phút|d|days?|ngày)$", args, re.IGNORECASE)
            if quick_match:
                amount = int(quick_match.group(1))
                unit = quick_match.group(2).lower()
                delta = timedelta(minutes=amount) if unit.startswith(("m", "p")) else (timedelta(days=amount) if unit.startswith(("d", "n")) else timedelta(hours=amount))
                until_utc = now_utc + delta
                setting.paused_until = until_utc
                db.commit()
                local_until = until_utc.astimezone(TZ).strftime("%I:%M %p (%H:%M)")
                await send_telegram_message(chat_id, f"⏸ *All reminders paused* until *{local_until}*.\nType `/resume` to turn back on.")
                return {"ok": True}

        # ----------------- LIST TASKS -----------------
        if text_content.startswith("/list"):
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()

            status_header = ""
            if setting and setting.paused_until and setting.paused_until > now_utc:
                local_pause = setting.paused_until.astimezone(TZ).strftime("%I:%M %p (%H:%M)")
                status_header = f"⏸ _All reminders are currently paused until {local_pause}_\n\n"

            if not tasks:
                await send_telegram_message(chat_id, f"{status_header}🎉 You have no pending tasks! You're all caught up.")
            else:
                lines = [f"{status_header}📋 *Your Pending Tasks:*\n"]
                for idx, t in enumerate(tasks, 1):
                    notes = []
                    if t.interval_mins and t.interval_mins != 60:
                        notes.append(f"repeat {format_interval_label(t.interval_mins)}")
                    if t.end_time:
                        notes.append(f"ends {t.end_time.astimezone(TZ).strftime('%H:%M')}")
                    if t.snooze_until and t.snooze_until > now_utc:
                        notes.append(f"snoozed until {t.snooze_until.astimezone(TZ).strftime('%H:%M')}")

                    note_str = f" _({', '.join(notes)})_" if notes else ""
                    lines.append(f"{idx}. *{t.task_text}*{note_str}")

                lines.append("\n💡 *Quick Controls:*")
                lines.append("• `done slides` or `done 1` ➔ Complete task")
                lines.append("• `pause slides 2h` or `pause 1 30m` ➔ Snooze specific task")
                await send_telegram_message(chat_id, "\n".join(lines))
            return {"ok": True}

        # ----------------- CUSTOM TYPED SNOOZE FOR LATEST TASK -----------------
        # E.g. '30m', '30p', '45 mins', '2h', 'until 5pm'
        typed_snooze = parse_typed_snooze(text_content, now_local)
        if typed_snooze:
            recent_task = db.query(Task).filter(
                Task.user_id == chat_id,
                Task.status == "pending"
            ).order_by(Task.last_reminded_at.desc().nullslast(), Task.id.desc()).first()

            if recent_task:
                recent_task.snooze_until = typed_snooze.astimezone(timezone.utc)
                db.commit()
                local_until = typed_snooze.strftime("%I:%M %p (%H:%M)")
                await send_telegram_message(
                    chat_id,
                    f"⏳ Snoozed: *\"{recent_task.task_text}\"* until *{local_until}*."
                )
                return {"ok": True}

        # ----------------- CREATE NEW STRUCTURED TASK -----------------
        # Parses: 'Finish slides - from 11:00 to 15:00 - snooze every 30m'
        # Or:     'Làm slide - từ 11h đến 15h - snooze mỗi 30p'
        task_name, start_t, end_t, interval_m = parse_structured_task(text_content, now_local)

        snooze_target = start_t.astimezone(timezone.utc) if start_t else None
        end_target = end_t.astimezone(timezone.utc) if end_t else None

        new_task = Task(
            user_id=chat_id,
            task_text=task_name,
            snooze_until=snooze_target,
            end_time=end_target,
            interval_mins=interval_m
        )
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        markup = make_task_markup(new_task.id, interval_m)

        # Build informative confirmation message
        details = [f"📝 *Added task:* \"{task_name}\""]
        if start_t:
            details.append(f"⏰ *Starts reminding at:* {start_t.strftime('%I:%M %p (%H:%M)')}")
        if end_t:
            details.append(f"🏁 *Ends at:* {end_t.strftime('%I:%M %p (%H:%M)')}")
        details.append(f"🔄 *Nag interval:* every {format_interval_label(interval_m)}")
        details.append("\n_(If no response within 2 mins when reminded, it automatically snoozes for 1 hour)_")

        await send_telegram_message(chat_id, "\n".join(details), reply_markup=markup)

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
        # Check global pause
        user_setting = db.query(UserSetting).filter(UserSetting.user_id == task.user_id).first()
        if user_setting and user_setting.paused_until and user_setting.paused_until > now_utc:
            continue

        # Check if task passed its end_time window (e.g. past 15h)
        if task.end_time and now_utc > task.end_time:
            continue

        task.remind_count += 1
        task.last_reminded_at = now_utc

        # Rule: If user takes no action / does not click snooze, automatically snooze every 1 hour!
        task.snooze_until = now_utc + timedelta(hours=1)
        db.commit()

        interval = task.interval_mins or 60
        lbl = format_interval_label(interval)
        markup = make_task_markup(task.id, interval)
        await send_telegram_message(
            chat_id=task.user_id,
            text=(
                f"🔔 *Reminder (Ping #{task.remind_count})*\n"
                f"Have you finished: *\"{task.task_text}\"*?\n\n"
                f"_(Auto-snoozes for 1 hour if no response in 2 mins. Tap Snooze for {lbl}, or reply with custom time)_"
            ),
            reply_markup=markup
        )
        nagged_count += 1

    return {
        "status": "success",
        "digest_slot": digest_slot,
        "nagged_tasks": nagged_count
    }
