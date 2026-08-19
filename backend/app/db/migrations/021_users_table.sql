-- Migration 021: Users table for authentication
-- Stores user credentials (bcrypt-hashed passwords) for email/password login.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS multi_store_rag_working.users (
    id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    username    VARCHAR(50)  UNIQUE NOT NULL,
    email       VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Fast lookup by email (login) and username (signup uniqueness check)
CREATE INDEX IF NOT EXISTS idx_users_email
    ON multi_store_rag_working.users(email);

CREATE INDEX IF NOT EXISTS idx_users_username
    ON multi_store_rag_working.users(username);
