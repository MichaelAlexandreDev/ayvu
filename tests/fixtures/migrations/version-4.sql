-- Released project DB with job, artifact and provenance tables. Never edit: see #138.
PRAGMA application_id=1096373845;
CREATE TABLE ayvu_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (1, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (2, 'bb8de968c66ff715f48f6fd763be29a553fd4c6a49b14c702dc07b1dabe5e5d9');
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (3, '936a09b9b40fa9d20d8687672b2dec067844c991747363be9d78b2a233b8a008');
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (4, 'f863172375c8ceceff484d694e2102c9a975f2041e198f73cb39bf3c23f7af28');
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
CREATE TABLE job_artifacts (
    artifact_id TEXT PRIMARY KEY NOT NULL,
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES translation_jobs(job_id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    stage TEXT NOT NULL,
    stage_version INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    media_type TEXT NOT NULL,
    location TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('staging', 'ready', 'abandoned')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    size INTEGER CHECK (size IS NULL OR size >= 0),
    digest TEXT,
    reason_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((size IS NULL) = (digest IS NULL)),
    CHECK (state <> 'ready' OR size IS NOT NULL),
    CHECK ((state = 'abandoned') = (reason_code IS NOT NULL))
);
CREATE UNIQUE INDEX job_artifacts_one_active
    ON job_artifacts(job_id, role, stage, stage_version, input_fingerprint)
    WHERE state IN ('staging', 'ready');
CREATE INDEX job_artifacts_by_state ON job_artifacts(state, created_at, artifact_id);
CREATE TRIGGER job_artifacts_immutable BEFORE UPDATE ON job_artifacts
WHEN OLD.state = 'abandoned'
    OR (OLD.state = 'ready' AND NEW.state <> 'abandoned')
    OR NEW.artifact_id IS NOT OLD.artifact_id
    OR NEW.schema_version IS NOT OLD.schema_version
    OR NEW.project_id IS NOT OLD.project_id
    OR NEW.job_id IS NOT OLD.job_id
    OR NEW.role IS NOT OLD.role
    OR NEW.stage IS NOT OLD.stage
    OR NEW.stage_version IS NOT OLD.stage_version
    OR NEW.input_fingerprint IS NOT OLD.input_fingerprint
    OR NEW.media_type IS NOT OLD.media_type
    OR NEW.location IS NOT OLD.location
    OR NEW.created_at IS NOT OLD.created_at
    OR (OLD.size IS NOT NULL AND (NEW.size IS NOT OLD.size OR NEW.digest IS NOT OLD.digest))
    OR NEW.revision <= OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'immutable_artifact');
END;
CREATE TABLE provenance_records (
    record_id TEXT PRIMARY KEY NOT NULL,
    schema_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES translation_jobs(job_id) ON DELETE RESTRICT,
    subject_namespace TEXT,
    subject_version INTEGER,
    subject_value TEXT,
    idempotency_key TEXT,
    supersedes TEXT UNIQUE REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    CHECK ((subject_namespace IS NULL) = (subject_value IS NULL)
           AND (subject_namespace IS NULL) = (subject_version IS NULL)),
    CHECK (supersedes IS NULL OR supersedes <> record_id)
);
CREATE UNIQUE INDEX provenance_records_idempotency
    ON provenance_records(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX provenance_records_by_job ON provenance_records(project_id, job_id, recorded_at, record_id);
CREATE INDEX provenance_records_by_subject
    ON provenance_records(project_id, subject_namespace, subject_version, subject_value, recorded_at, record_id);
CREATE TABLE provenance_parents (
    record_id TEXT NOT NULL REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    parent_id TEXT NOT NULL REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    PRIMARY KEY (record_id, parent_id),
    CHECK (record_id <> parent_id)
) WITHOUT ROWID;
CREATE TABLE provenance_artifacts (
    record_id TEXT NOT NULL REFERENCES provenance_records(record_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES job_artifacts(artifact_id) ON DELETE RESTRICT,
    PRIMARY KEY (record_id, artifact_id)
) WITHOUT ROWID;
CREATE INDEX provenance_artifacts_by_artifact ON provenance_artifacts(artifact_id, record_id);
CREATE TRIGGER provenance_records_no_update BEFORE UPDATE ON provenance_records
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END;
CREATE TRIGGER provenance_records_no_delete BEFORE DELETE ON provenance_records
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END;
CREATE TRIGGER provenance_parents_no_update BEFORE UPDATE ON provenance_parents
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END;
CREATE TRIGGER provenance_parents_no_delete BEFORE DELETE ON provenance_parents
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END;
CREATE TRIGGER provenance_artifacts_no_update BEFORE UPDATE ON provenance_artifacts
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END;
CREATE TRIGGER provenance_artifacts_no_delete BEFORE DELETE ON provenance_artifacts
BEGIN
    SELECT RAISE(ABORT, 'immutable_provenance');
END;
PRAGMA user_version=4;
