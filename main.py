import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends
import httpx
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from database import SessionLocal, Task

load_dotenv()

app = FastAPI(title="Telegram Persistent Reminder Bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRON_SECRET = os.getenv("CRON_SECRET", "super-secret-key-123")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

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

@app.get("/health")
def health():
    return {"status": "ok"}

# ----------------- TELEGRAM WEBHOOK -----------------
@app.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    # 1. Handle Inline Button Clicks (Done / Snooze)
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        action_data = cb["data"]  # e.g., "done_12" or "snooze_12_60"

        parts = action_data.split("_")
        action = parts[0]
        task_id = int(parts[1])

        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            await answer_callback_query(cb_id, "Task not found.")
            return {"ok": True}

        if action == "done":
            task.status = "completed"
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            await answer_callback_query(cb_id, "Marked as done! 🎉")
            await send_telegram_message(chat_id, f"✅ *Completed:* \"{task.task_text}\"\nAwesome job!")

        elif action == "snooze":
            minutes = int(parts[2])
            task.snooze_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            db.commit()
            await answer_callback_query(cb_id, f"Snoozed for {minutes}m ⏰")
            await send_telegram_message(chat_id, f"⏳ Snoozed: \"{task.task_text}\" for {minutes} mins.")

        return {"ok": True}

    # 2. Handle Text Messages
    if "message" in data and "text" in data["message"]:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()

        if text.startswith("/start"):
            welcome = (
                "👋 *Welcome to your Persistent Reminder Bot!*\n\n"
                "• Send me any task to add it (e.g. `Finish presentation slides`)\n"
                "• Type `/list` to view all pending tasks\n"
                "• I will keep nagging you until you tap *Done*!"
            )
            await send_telegram_message(chat_id, welcome)
            return {"ok": True}

        if text.startswith("/list"):
            tasks = db.query(Task).filter(Task.user_id == chat_id, Task.status == "pending").all()
            if not tasks:
                await send_telegram_message(chat_id, "🎉 You have no pending tasks! Enjoy your free time.")
            else:
                lines = ["📋 *Your Pending Tasks:*\n"]
                now_utc = datetime.now(timezone.utc)
                for t in tasks:
                    status_note = " (Snoozed)" if t.snooze_until and t.snooze_until > now_utc else ""
                    lines.append(f"• *{t.task_text}*{status_note}")
                await send_telegram_message(chat_id, "\n".join(lines))
            return {"ok": True}

        # Any plain message is saved as a new task
        new_task = Task(user_id=chat_id, task_text=text)
        db.add(new_task)
        db.commit()
        db.refresh(new_task)

        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ Done", "callback_data": f"done_{new_task.id}"},
                    {"text": "⏰ Snooze 1h", "callback_data": f"snooze_{new_task.id}_60"}
                ]
            ]
        }
        await send_telegram_message(
            chat_id,
            f"📝 *Added task:* \"{text}\"\nI will nag you periodically until it's finished!",
            reply_markup=markup
        )

    return {"ok": True}

# ----------------- CRON REMINDER TRIGGER -----------------
@app.get("/cron/nag")
async def cron_nag(secret: str, db: Session = Depends(get_db)):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    now = datetime.now(timezone.utc)

    # Find pending tasks whose snooze time has passed (or was never set)
    tasks = db.query(Task).filter(
        Task.status == "pending",
        (Task.snooze_until == None) | (Task.snooze_until <= now)
    ).all()

    for task in tasks:
        task.remind_count += 1
        task.last_reminded_at = now
        # Auto-snooze for 1 hour so it doesn't repeatedly ping every 15-minute cron execution
        task.snooze_until = now + timedelta(hours=1)
        db.commit()

        markup = {
            "inline_keyboard": [
                [{"text": "✅ Done", "callback_data": f"done_{task.id}"}],
                [
                    {"text": "⏰ 30m", "callback_data": f"snooze_{task.id}_30"},
                    {"text": "⏰ 2h", "callback_data": f"snooze_{task.id}_120"}
                ]
            ]
        }

        await send_telegram_message(
            chat_id=task.user_id,
            text=f"🔔 *Reminder (Ping #{task.remind_count})*\nHave you finished: *\"{task.task_text}\"*?",
            reply_markup=markup
        )

    return {"status": "reminded", "count": len(tasks)}
