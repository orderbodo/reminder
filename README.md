# Telegram Persistent Reminder Bot (Supabase + Render + Cron)

A persistent nagging reminder Telegram bot that keeps reminding you about your to-do items with interactive buttons (`✅ Done`, `⏰ Snooze`) until you mark them complete.

---

## 📁 Project Structure

```
D:/Work/AI/telegram-reminder-bot/
├── .env.example       # Template for environment variables
├── requirements.txt   # Python dependencies
├── database.py        # Supabase PostgreSQL model & connection
├── main.py            # FastAPI Webhook & Cron nag endpoint
├── schema.sql         # SQL script to run in Supabase
└── README.md          # Setup & deployment guide
```

---

## 🚀 Quick Setup Guide

### 1. Supabase Database
1. Create a free project at [supabase.com](https://supabase.com).
2. Go to **SQL Editor** and execute the queries in `schema.sql`.
3. In **Project Settings** -> **Database**, copy your PostgreSQL URI string.

### 2. Local Testing (Optional)
1. Create a `.env` file from `.env.example` and fill in your credentials.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the development server:
   ```bash
   uvicorn main:app --reload
   ```

### 3. Deploy to Render
1. Push this folder to a GitHub repository.
2. In [Render Dashboard](https://dashboard.render.com), create a new **Web Service** connected to your repo:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
3. Add Environment Variables in Render:
   - `DATABASE_URL`: Your Supabase connection string.
   - `TELEGRAM_BOT_TOKEN`: Token from `@BotFather`.
   - `CRON_SECRET`: A secret string of your choice.

### 4. Activate Telegram Webhook
Open in your browser (replace values with your own):
```text
https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<YOUR-RENDER-APP>.onrender.com/webhook
```

### 5. Schedule Reminders on cron-job.org
1. Create a free account at [cron-job.org](https://cron-job.org).
2. Create a new job:
   - **URL**: `https://<YOUR-RENDER-APP>.onrender.com/cron/nag?secret=<YOUR_CRON_SECRET>`
   - **Interval**: Every 15 or 30 minutes.
