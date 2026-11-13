-- Run this in the Supabase SQL Editor:
CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    task_text TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending', -- 'pending', 'completed', 'cancelled'
    snooze_until TIMESTAMPTZ NULL,
    end_time TIMESTAMPTZ NULL,
    interval_mins INT DEFAULT 60,
    remind_count INT DEFAULT 0,
    last_reminded_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL
);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS interval_mins INT DEFAULT 60;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS end_time TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks (user_id, status, snooze_until);

-- Tracks daily digests so they are sent only once per schedule window
CREATE TABLE IF NOT EXISTS digest_logs (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    slot_key VARCHAR(50) NOT NULL,
    sent_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_digest_user_slot ON digest_logs (user_id, slot_key);

-- Stores global pause / snooze preferences per user
CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY,
    paused_until TIMESTAMPTZ NULL
);
