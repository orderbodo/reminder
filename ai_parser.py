import os
import json
from datetime import datetime
from typing import List, Optional
import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_PROMPT = """You are the AI brain of a Telegram persistent reminder bot.
The user talks naturally in English, Vietnamese, or mixed.
Your job is to understand their intent and convert it into a JSON object: {"actions": [ ... ]}

Action types you can emit:
1. {"type":"add_task","task":"Clean task name","start_iso":null|"ISO8601","end_iso":null|"ISO8601","interval_mins":int}
   - start_iso: when reminders should start (null = remind immediately).
   - end_iso: deadline/end window when reminders should stop (null = none).
   - interval_mins: repeat/nag interval in minutes (default 60 if not specified).
2. {"type":"done","task_id":int}            -> user completed a task.
3. {"type":"delete","task_id":int}          -> user wants to remove/cancel a task.
4. {"type":"snooze","task_id":int,"until_iso":"ISO8601"} -> delay/snooze a specific task.
5. {"type":"pause_all","until_iso":"ISO8601"} -> silence all reminders until a specific time.
6. {"type":"resume_all"}                    -> unpause/resume all reminders now.
7. {"type":"list"}                          -> user asks to see their tasks.
8. {"type":"chat","reply":"friendly brief response"} -> greetings, general chat, or asking clarification.

Rules:
- Match task_id using the pending tasks list by meaning (e.g. "done slide" matches task "Finish quarterly slide deck").
- All times must be resolved to ISO8601 with the local timezone offset provided in the context.
- "every 30m" / "mỗi 30p" -> interval_mins = 30. Default is 60.
- If a time has already passed today, assume tomorrow or the upcoming occurrence.
- Keep task names clean (strip out time words like "at 5pm" or "từ 11h").
- Return strictly valid JSON.
"""

async def diagnose() -> str:
    """Tests the Gemini API connection live."""
    if not GEMINI_API_KEY:
        return "❌ GEMINI_API_KEY is not set in Render Environment variables."
    payload = {"contents": [{"role": "user", "parts": [{"text": "Reply with: OK"}]}]}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(GEMINI_URL, params={"key": GEMINI_API_KEY}, json=payload)
        if resp.status_code == 200:
            return f"✅ AI is working perfectly!\nModel: {GEMINI_MODEL}"
        try:
            err = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            err = resp.text
        return f"❌ Gemini Error (HTTP {resp.status_code}): {err[:300]}"
    except Exception as e:
        return f"❌ Could not connect to Gemini: {e}"

async def interpret(text: str, now_local: datetime, pending_tasks: List[dict]) -> Optional[List[dict]]:
    """Sends user text + context to Gemini and returns structured actions."""
    if not GEMINI_API_KEY:
        return None

    tasks_context = "\n".join(f"- ID {t['id']}: {t['text']}" for t in pending_tasks) or "(None)"
    user_context = (
        f"Current local time: {now_local.isoformat(timespec='minutes')} ({now_local.strftime('%A')})\n"
        f"Pending tasks for this user:\n{tasks_context}\n\n"
        f"User message: {text}"
    )

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_context}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(GEMINI_URL, params={"key": GEMINI_API_KEY}, json=payload)
            resp.raise_for_status()
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(raw)
            actions = data.get("actions", [])
            return actions if isinstance(actions, list) else None
    except Exception as e:
        print(f"Gemini interpret error: {e}")
        return None
