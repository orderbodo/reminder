import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List
from fastapi import FastAPI, Request, HTTPException, Depends
import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import SessionLocal, Task, DigestLog, UserSetting, Base, engine
import ai_parser

load_dotenv()

# Auto-create tables / columns
try:
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS interval_mins INT DEFAULT 60;"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ NULL;"))
        conn.commit()
except Exception as e:
    print(f"Database note: {e}")

app = FastAPI(title="Telegram Persistent Reminder Bot (AI-Powered)")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET", "super-secret-key-123")
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
        {"command": "list", "description": "📋 View pending tasks"},
        {"command": "pause", "description": "⏸ Pause all reminders"},
        {"command": "resume", "description": "▶️ Resume all reminders"},
        {"command": "ai", "description": "🤖 Test Gemini AI status"},
        {"command": "start", "description": "👋 Help & introduction"}
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

async def send_telegram_message(chat_id: int, message_text: str, reply_markup: Optional[dict] = None):
    async with httpx.AsyncClient() as client:
        payload = {"chat_id": chat_id, "text": message_text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)

async def answer_callback_query(callback_query_id: str, feedback: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": feedback
        })

def format_interval_label(minutes: int) -> str:
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

def clean_text(s: str) -> str:
    return re.sub(r"[*_`\[\]]", "", str(s or "")).strip()

def parse_iso(val) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt
    except Exception:
        return None

def execute_ai_actions(db: Session, chat_id: int, actions: List[dict]) -> List[str]:
    replies = []
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(TZ)

    for act in actions:
        kind = act.get("type")

        if kind == "add_task":
            name = clean_text(act.get("task"))
            if not name:
                continue
            start_dt = parse_iso(act.get("start_iso"))
            end_dt = parse_iso(act.get("end_iso"))
            interval = act.get("interval_mins") or 60
            interval = int(interval) if int(interval) > 0 else 60

            snooze_until = start_dt.astimezone(timezone.utc) if start_dt and start_dt > now_local else None
            end_time = end_dt.astimezone(timezone.utc) if end_dt else None

            task = Task(
                user_id=chat_id,
                task_text=name,
                snooze_until=snooze_until,
                end_time=end_time,
                interval_mins=interval
            )
            db.add(task)
            db.commit()

            msg = f"📝 *Added task:* \"{name}\""
            if start_dt and start_dt > now_local:
                msg += f"\n⏰ *Starts:* {start_dt.astimezone(TZ).strftime('%I:%M %p (%a)')}"
            if end_dt:
                msg += f"\n🏁 *Deadline:* {end_dt.astimezone(TZ).strftime('%I:%M %p (%a)')}"
            msg += f"\n🔄 *Nag interval:* every {format_interval_label(interval)}"
            replies.append(msg)

        elif kind in ("done", "delete", "snooze"):
            task_id = act.get("task_id")
            task = db.query(Task).filter(Task.id == task_id, Task.user_id == chat_id, Task.status == "pending").first()
            if not task:
                replies.append("⚠️ Task not found. Type `/list` to check open tasks.")
                continue

            if kind == "done":
                task.status = "completed"
                task.completed_at = now_utc
                replies.append(f"✅ *Marked completed:* \"{clean_text(task.task_text)}\"! 🎉")
            elif kind == "delete":
                task.status = "cancelled"
                replies.append(f"🗑 *Deleted task:* \"{clean_text(task.task_text)}\".")
            elif kind == "snooze":
                until_dt = parse_iso(act.get("until_iso"))
                if until_dt:
                    task.snooze_until = until_dt.astimezone(timezone.utc)
                    replies.append(f"⏳ *Snoozed:* \"{clean_text(task.task_text)}\" until {until_dt.astimezone(TZ).strftime('%I:%M %p')}.")
                else:
                    task.snooze_until = now_utc + timedelta(hours=1)
                    replies.append(f"⏳ *Snoozed:* \"{clean_text(task.task_text)}\" for 1 hour.")
            db.commit()

        elif kind == "pause_all":
            until_dt = parse_iso(act.get("until_iso")) or (now_utc + timedelta(hours=1))
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if not setting:
                setting = UserSetting(user_id=chat_id)
                db.add(setting)
            setting.paused_until = until_dt.astimezone(timezone.utc)
            db.commit()
            replies.append(f"⏸ *All reminders paused* until {until_dt.astimezone(TZ).strftime('%I:%M %p')}.\nUse `/resume` to turn back on.")

        elif kind == "resume_all":
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if setting:
                setting.paused_until = None
                db.commit()
            replies.append("▶️ *All reminders resumed!*")

        elif kind == "list":
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
            if not tasks:
                replies.append("🎉 You have no pending tasks! You're all caught up.")
            else:
                lines = ["📋 *Your Pending Tasks:*\n"]
                for i, t in enumerate(tasks, 1):
                    snooze_note = f" _(snoozed until {t.snooze_until.astimezone(TZ).strftime('%H:%M')})_" if t.snooze_until and t.snooze_until > now_utc else ""
                    lines.append(f"{i}. *{clean_text(t.task_text)}*{snooze_note}")
                replies.append("\n".join(lines))

        elif kind == "chat":
            replies.append(clean_text(act.get("reply")) or "I'm ready! Send me any task to remember.")

    return replies

@app.get("/")
@app.get("/health")
def health():
    return {
        "status": "ok",
        "message": "Telegram Reminder Bot is running!",
        "ai_key_configured": bool(ai_parser.GEMINI_API_KEY),
        "ai_model": ai_parser.GEMINI_MODEL
    }

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
                await send_telegram_message(chat_id, "▶️ *Reminders resumed!*")
            else:
                minutes = int(val)
                until_utc = now_utc + timedelta(minutes=minutes)
                setting.paused_until = until_utc
                db.commit()
                local_until = until_utc.astimezone(TZ).strftime("%I:%M %p")
                await answer_callback_query(cb_id, f"Paused ⏸")
                await send_telegram_message(chat_id, f"⏸ *Reminders paused* until *{local_until}*.\nType `/resume` to turn back on.")
            return {"ok": True}

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
            await send_telegram_message(chat_id, f"✅ *Completed:* \"{task.task_text}\"\nGreat work! 🎉")

        elif action == "delete":
            task.status = "cancelled"
            db.commit()
            await answer_callback_query(cb_id, "Deleted.")
            await send_telegram_message(chat_id, f"🗑 *Deleted task:* \"{task.task_text}\"")

        elif action == "snooze":
            interval = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else (task.interval_mins or 60)
            task.snooze_until = now_utc + timedelta(minutes=interval)
            db.commit()
            local_until = task.snooze_until.astimezone(TZ).strftime("%I:%M %p")
            lbl = format_interval_label(interval)
            await answer_callback_query(cb_id, f"Snoozed for {lbl} ⏰")
            await send_telegram_message(chat_id, f"⏳ Snoozed *\"{task.task_text}\"* for {lbl} (until {local_until}).")

        return {"ok": True}

    # 2. Handle Text Messages
    if "message" in data and "text" in data["message"]:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text_content = msg["text"].strip()
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now(TZ)

        if text_content.startswith("/start"):
            welcome = (
                "👋 *Persistent Reminder Bot (AI-Powered)*\n\n"
                "I understand natural speech in English, Vietnamese, or mixed! Just talk to me:\n\n"
                "💡 *Examples:*\n"
                "• *Add:* \"Remind me to finish slides from 2pm to 5pm, nag every 30m\"\n"
                "• *Add:* \"Uống nước mỗi 45 phút\"\n"
                "• *Done:* \"Done with the slides\" or \"Xong việc rồi\"\n"
                "• *Snooze:* \"Snooze slides until 8pm\"\n\n"
                "⏰ *Persistent Reminders:*\n"
                "• When I remind you: tap `[✅ Done]` or `[⏰ Snooze]`.\n"
                "• If you don't reply within 2 mins: **auto-snoozes for 1 hour**!\n"
                "• 🌙 Quiet Hours: 10:00 PM – 9:00 AM\n\n"
                "📋 *Commands:* `/list` • `/pause` • `/resume` • `/ai`"
            )
            await send_telegram_message(chat_id, welcome)
            return {"ok": True}

        if text_content.startswith("/ai"):
            diag = await ai_parser.diagnose()
            await send_telegram_message(chat_id, f"🤖 *AI Diagnostic Status:*\n{diag}")
            return {"ok": True}

        if text_content.startswith("/resume"):
            setting = db.query(UserSetting).filter(UserSetting.user_id == chat_id).first()
            if setting:
                setting.paused_until = None
                db.commit()
            await send_telegram_message(chat_id, "▶️ *All reminders resumed!*")
            return {"ok": True}

        if text_content.startswith("/pause"):
            await send_telegram_message(
                chat_id,
                "⏸ *Pause Reminders:*\nChoose how long to silence all reminders:",
                reply_markup=make_pause_markup()
            )
            return {"ok": True}

        if text_content.startswith("/list"):
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
            if not tasks:
                await send_telegram_message(chat_id, "🎉 You have no pending tasks! You're all caught up.")
            else:
                lines = ["📋 *Your Pending Tasks:*\n"]
                for i, t in enumerate(tasks, 1):
                    snooze_note = f" _(snoozed until {t.snooze_until.astimezone(TZ).strftime('%I:%M %p')})_" if t.snooze_until and t.snooze_until > now_utc else ""
                    lines.append(f"{i}. *{clean_text(t.task_text)}*{snooze_note}")
                await send_telegram_message(chat_id, "\n".join(lines))
            return {"ok": True}

        # ----------------- AI ROUTING (Gemini) -----------------
        pending_tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").order_by(Task.id.asc()).all()
        ai_actions = await ai_parser.interpret(
            text_content,
            now_local,
            [{"id": t.id, "text": t.task_text} for t in pending_tasks]
        )

        if ai_actions:
            replies = execute_ai_actions(db, chat_id, ai_actions)
            if replies:
                await send_telegram_message(chat_id, "\n\n".join(replies))
                return {"ok": True}

        # Fallback if AI key is missing or call fails: save message directly as a task
        new_task = Task(user_id=chat_id, task_text=text_content, interval_mins=60)
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        markup = make_task_markup(new_task.id, 60)
        await send_telegram_message(
            chat_id,
            f"📝 *Added task:* \"{text_content}\"\nI will nag you periodically until it's finished!",
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

        if task.end_time and now_utc > task.end_time:
            continue

        task.remind_count += 1
        task.last_reminded_at = now_utc

        # Auto-snooze for 1 hour by default if ignored
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
                f"_(Auto-snoozes for 1h if no reply in 2 mins. Tap Snooze for {lbl}, or reply to customize)_"
            ),
            reply_markup=markup
        )
        nagged_count += 1

    return {
        "status": "success",
        "digest_slot": digest_slot,
        "nagged_tasks": nagged_count
    }
