-- ============================================================================
--  multiai.online — reset
--  Destroys every table and all data. Run 001_schema.sql afterwards.
--  Order matters only if you drop without CASCADE; CASCADE is used here.
-- ============================================================================

BEGIN;

DROP TABLE IF EXISTS messages       CASCADE;
DROP TABLE IF EXISTS discussions    CASCADE;
DROP TABLE IF EXISTS group_members  CASCADE;
DROP TABLE IF EXISTS "groups"       CASCADE;
DROP TABLE IF EXISTS agents         CASCADE;
DROP TABLE IF EXISTS credentials    CASCADE;
DROP TABLE IF EXISTS users          CASCADE;
DROP TABLE IF EXISTS schema_version CASCADE;

DROP FUNCTION IF EXISTS utcnow();

COMMIT;
