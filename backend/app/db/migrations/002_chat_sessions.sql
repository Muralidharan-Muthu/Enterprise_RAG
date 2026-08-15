-- Chat sessions for Multi-Store RAG Chatbot
-- Run via Supabase SQL Editor after 001_initial_schema.sql
-- Schema: multi_store_rag_working

SET search_path TO multi_store_rag_working, public;

-- ── Chat sessions ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_sessions (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title         TEXT        NOT NULL DEFAULT 'New Chat',
    message_count INT         NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated_at
    ON chat_sessions (updated_at DESC);

-- ── Chat messages ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT        NOT NULL,
    -- assistant-only fields
    confidence      FLOAT,
    processing_time FLOAT,
    stores_searched TEXT[],
    notes           TEXT,
    citations       JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages (session_id, created_at ASC);

-- ── Trigger: keep session stats in sync ───────────────────────────────────────

CREATE OR REPLACE FUNCTION update_chat_session_stats()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE chat_sessions
    SET updated_at    = NOW(),
        message_count = message_count + 1
    WHERE id = NEW.session_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chat_message_inserted ON chat_messages;
CREATE TRIGGER trg_chat_message_inserted
    AFTER INSERT ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION update_chat_session_stats();
