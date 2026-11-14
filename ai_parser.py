"""
AI layer: turns any free-form message (English / Vietnamese / mixed) into structured actions.
Uses the Gemini REST API via httpx (no extra dependency).
If the key is missing or the call fails, main.py falls back to the rule-based parser.
"""
import json
import os
from datetime import datetime
from typing import List, Optional

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_PROMPT = """You are the brain of a Telegram to-do reminder bot. The user writes in English, Vietnamese, or a mix.
Convert the user's message into a JSON object: {"actions": [ ... ]}. One message may contain several actions.

Action types (use exactly these keys):
1. {"type":"add_task","task":"short clean task name","start_iso":null|"ISO8601","end_iso":null|"ISO8601","interval_mins":null|int}
   - start_iso: when reminders should START (null = remind immediately).
   - end_iso: when reminders should STOP (null = until done).
   - interval_mins: how often to nag (null = 60).
2. {"type":"done","task_id":int}            - user finished a task
3. {"type":"delete","task_id":int}          - user wants to cancel/remove a task
4. {"type":"snooze","task_id":int,"until_iso":"ISO8601"}  - delay ONE task
5. {"type":"set_interval","task_id":int,"interval_mins":int} - change how often a task nags
6. {"type":"list"}                          - show pending tasks
7. {"type":"pause_all","until_iso":"ISO8601"} - silence ALL reminders
8. {"type":"resume_all"}                    - turn reminders back on
9. {"type":"chat","reply":"short helpful reply"} - anything else / unclear / needs clarification

Rules:
- Use the pending task list (with ids) to pick task_id. Match by meaning, not exact words
  (e.g. "xong slide rồi" -> the slide task). If nothing matches, use "chat" and say so.
- All times are in the user's timezone. Use the provided current local time to resolve
  "tonight", "tomorrow", "in 2 hours", "chiều nay", "mai 9h", "từ 11h đến 15h", etc.
  Output ISO8601 WITH the UTC offset given in the current time.
- If a time of day already passed today, assume the next occurrence.
- "every 30 minutes" / "mỗi 30p" -> interval_mins=30. "mỗi tiếng" -> 60.
- Keep "task" short, in the user's own language, without time words.
- Reply in the same language as the user for "chat" replies. Keep replies under 2 sentences.
- Output ONLY valid JSON. No markdown fences, no commentary.
"""


async def interpret(text: str, now_local: datetime, pending_tasks: List[dict]) -> Optional[List[dict]]:
    """Returns a list of action dicts, or None if the AI is unavailable / failed."""
    if not GEMINI_API_KEY:
        return None

    task_lines = "\n".join(f'{t["id"]}: {t["text"]}' for t in pending_tasks) or "(none)"
    user_block = (
        f"Current local time: {now_local.isoformat(timespec='minutes')} ({now_local.strftime('%A')})\n"
        f"Pending tasks:\n{task_lines}\n\n"
        f"User message: {text}"
    )

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_block}]}],
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
        print(f"AI interpret failed, falling back to rules: {e}")
        return None
