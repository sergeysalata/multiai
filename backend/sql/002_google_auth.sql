-- ============================================================================
--  multiai.online — migration 002: Google sign-in
--  Run in pgAdmin against the "multiai" database, after 001_schema.sql.
--  Idempotent: safe to run twice.
-- ============================================================================
--
--  Adds:
--    users.google_sub      Google's stable account id ("sub" claim)
--    users.avatar_url      profile picture from Google
--    users.last_login_at   for spotting dormant accounts
--  Changes:
--    users.password_hash   becomes nullable — an account created through
--                          Google has no password at all, and storing a
--                          dummy hash would let someone try to guess it.
-- ============================================================================

BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub    varchar(64);
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url    varchar(512) NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at timestamp;

ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_users_google_sub'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT uq_users_google_sub UNIQUE (google_sub);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_users_google_sub ON users (google_sub)
    WHERE google_sub IS NOT NULL;

-- An account must be reachable by at least one method.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_login_method'
    ) THEN
        ALTER TABLE users ADD CONSTRAINT ck_users_login_method
            CHECK (password_hash IS NOT NULL OR google_sub IS NOT NULL);
    END IF;
END
$$;

INSERT INTO schema_version (version, note)
VALUES (2, 'google sign-in: google_sub, avatar_url, last_login_at')
ON CONFLICT (version) DO NOTHING;

COMMIT;
