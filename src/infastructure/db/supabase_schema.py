"""
SQL DDL for the Gift Concierge Agent database.

Tables
------
st_turns            Tier 1 — short-term conversation ring buffer
recipient_profiles  Tier 3 — semantic recipient profiles (JSON columns)

Run via:  python scripts/init_supabase.py
"""

# ---------------------------------------------------------------------------
# Tier 1 — Short-Term Memory: conversation turns ring buffer
# ---------------------------------------------------------------------------

CREATE_ST_TURNS = """
CREATE TABLE IF NOT EXISTS st_turns (
    id          BIGSERIAL     PRIMARY KEY,
    user_id     TEXT          NOT NULL,
    session_id  TEXT          NOT NULL,
    role        TEXT          NOT NULL
                              CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content     TEXT          NOT NULL,
    ts          FLOAT8        NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
"""

CREATE_ST_TURNS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_st_turns_user_session
    ON st_turns (user_id, session_id);

CREATE INDEX IF NOT EXISTS idx_st_turns_ts
    ON st_turns (ts);
"""

# ---------------------------------------------------------------------------
# Tier 3 — Semantic Long-Term Memory: recipient profiles
# ---------------------------------------------------------------------------

CREATE_RECIPIENT_PROFILES = """
CREATE TABLE IF NOT EXISTS recipient_profiles (
    profile_id          TEXT    PRIMARY KEY,
    user_id             TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    relationship        TEXT,
    preferences         JSONB   NOT NULL DEFAULT '[]',
    dislikes            JSONB   NOT NULL DEFAULT '[]',
    budget_lkr          FLOAT8,
    upcoming_occasions  JSONB   NOT NULL DEFAULT '[]',
    past_gifts          JSONB   NOT NULL DEFAULT '[]',
    notes               TEXT,
    created_at          FLOAT8  NOT NULL DEFAULT 0,
    updated_at          FLOAT8  NOT NULL DEFAULT 0
);
"""

CREATE_RECIPIENT_PROFILES_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_user_name
    ON recipient_profiles (user_id, LOWER(name));

CREATE INDEX IF NOT EXISTS idx_profiles_user_id
    ON recipient_profiles (user_id);
"""

# ---------------------------------------------------------------------------
# Convenience: ordered list of all DDL statements to execute
# ---------------------------------------------------------------------------

ALL_DDL: list[str] = [
    CREATE_ST_TURNS,
    CREATE_ST_TURNS_INDEXES,
    CREATE_RECIPIENT_PROFILES,
    CREATE_RECIPIENT_PROFILES_INDEXES,
]
