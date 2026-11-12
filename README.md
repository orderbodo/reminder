# Telegram Persistent Reminder Bot (Supabase + Render + Cron)

A persistent reminder Telegram bot that keeps reminding you about your to-dos with flexible snoozing options and automated daily digests at **9:30 AM**, **1:30 PM**, and **6:30 PM**.

---

## ✨ Features

- **Quick Task Capture**: Simply type any message to your bot to create a task.
- **Multiple Snooze Options**:
  - `[✅ Done]`
  - `[🗑 Delete]`
  - `[⏰ 15m]`, `[⏰ 30m]`, `[⏰ 1h]`, `[⏰ 3h]`
  - `[🌙 Tonight (8 PM)]`, `[☀️ Tomorrow (9:30 AM)]`
- **Automated Daily Summaries**:
  - ☀️ **9:30 AM**: Morning kick-off & daily agenda.
  - ⚡ **1:30 PM**: Midday progress check-in.
  - 🌙 **6:30 PM**: Evening wrap-up of outstanding items.
- **100% Free & Stable Cloud Architecture**:
  - FastAPI Webhook hosted on **Render**.
  - PostgreSQL database on **Supabase** (IPv4 Session Pooler).
  - Accurate scheduled triggers via **cron-job.org**.

---

## ⚙️ Environment Variables (Render)

| Variable | Description | Example |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Bot API token from `@BotFather` | `9087868915:AAEx_...` |
| `DATABASE_URL` | Supabase IPv4 Session Pooler URI | `postgresql://postgres.xxx:pass@aws-0-xx.pooler.supabase.com:6543/postgres` |
| `CRON_SECRET` | Secret token protecting the cron endpoint | `your-secret-password` |
| `USER_TIMEZONE` | Timezone for daily digests (Default: `Asia/Bangkok` UTC+7) | `Asia/Bangkok` |
