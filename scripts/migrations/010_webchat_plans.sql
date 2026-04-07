-- Migration: 010_webchat_plans
-- Agent planning mode: task plans in webchat conversations

CREATE TABLE IF NOT EXISTS chat.webchat_plans (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES chat.webchat_conversations(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by TEXT NOT NULL,
    auto_continue_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_webchat_plans_conv
    ON chat.webchat_plans(conversation_id);

CREATE TABLE IF NOT EXISTS chat.webchat_plan_tasks (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL
        REFERENCES chat.webchat_plans(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_webchat_plan_tasks_plan
    ON chat.webchat_plan_tasks(plan_id);
