-- ============================================================================
--  multiai.online — migration 003: rooms instead of one-shot debates
--  Run in pgAdmin against the "multiai" database, after 002_google_auth.sql.
--  Idempotent: safe to run twice.
-- ============================================================================
--
--  A conversation no longer ends. After the agents have each spoken, the room
--  goes to 'idle' and waits for you, instead of going to 'done' and closing.
--  'done' stays permitted so existing rows keep validating.
-- ============================================================================

BEGIN;

ALTER TABLE discussions DROP CONSTRAINT IF EXISTS ck_discussions_status;

ALTER TABLE discussions ADD CONSTRAINT ck_discussions_status
    CHECK (status IN ('queued', 'running', 'idle', 'done', 'failed', 'cancelled'));

-- Old finished debates become ordinary rooms you can carry on talking in.
UPDATE discussions SET status = 'idle' WHERE status IN ('done', 'cancelled');

-- The 'rounds' column is no longer a limit. It is kept only so old rows and
-- the column default stay valid; nothing reads it any more.
COMMENT ON COLUMN discussions.rounds IS
    'Unused since migration 003. Conversations run until you stop them.';

ALTER TABLE discussions ALTER COLUMN stage SET DEFAULT 'Waiting for you';

-- Conversation mode.
--   step: every agent speaks once, then the room waits for you.
--   flow: the agents keep talking to each other until you stop them.
ALTER TABLE discussions ADD COLUMN IF NOT EXISTS mode varchar(8) NOT NULL DEFAULT 'step';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_discussions_mode') THEN
        ALTER TABLE discussions ADD CONSTRAINT ck_discussions_mode
            CHECK (mode IN ('step', 'flow'));
    END IF;
END
$$;

INSERT INTO schema_version (version, note)
VALUES (3, 'rooms: idle state, step/flow modes, no rounds, no verdict')
ON CONFLICT (version) DO NOTHING;

COMMIT;
