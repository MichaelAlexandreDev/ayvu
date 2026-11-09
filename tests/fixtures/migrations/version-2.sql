-- Released project DB with the translation job tables. Never edit: see #136.
PRAGMA application_id=1096373845;
CREATE TABLE ayvu_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (1, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (2, 'bb8de968c66ff715f48f6fd763be29a553fd4c6a49b14c702dc07b1dabe5e5d9');
CREATE TABLE translation_jobs (
    job_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    command_version INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    output_roles TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'preflight', 'running', 'pausing', 'paused', 'cancelling', 'cancelled',
        'partially_completed', 'failed', 'completed', 'validating', 'validated')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_owner_token TEXT,
    lease_acquired_at TEXT,
    lease_expires_at TEXT,
    terminal_summary TEXT,
    continuation_of TEXT UNIQUE REFERENCES translation_jobs(job_id) ON DELETE RESTRICT,
    CHECK ((lease_owner_token IS NULL) = (lease_acquired_at IS NULL)
           AND (lease_owner_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK (lease_owner_token IS NULL
           OR state NOT IN ('validated', 'partially_completed', 'failed', 'cancelled')),
    CHECK (terminal_summary IS NULL
           OR state IN ('validated', 'partially_completed', 'failed', 'cancelled')),
    CHECK (continuation_of IS NULL OR continuation_of <> job_id)
);
CREATE INDEX translation_jobs_by_project_state
    ON translation_jobs(project_id, state, created_at, job_id);
CREATE TRIGGER translation_jobs_immutable BEFORE UPDATE ON translation_jobs
WHEN OLD.state IN ('validated', 'partially_completed', 'failed', 'cancelled')
    OR NEW.job_id IS NOT OLD.job_id
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.command_type IS NOT OLD.command_type
    OR NEW.command_version IS NOT OLD.command_version
    OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
    OR NEW.output_roles IS NOT OLD.output_roles
    OR NEW.created_at IS NOT OLD.created_at
    OR NEW.continuation_of IS NOT OLD.continuation_of
    OR NEW.revision < OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'immutable_job');
END;
PRAGMA user_version=2;
