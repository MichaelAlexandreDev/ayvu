-- Initial infrastructure-only project DB. No invented historical domain tables.
PRAGMA application_id=1096373845;
CREATE TABLE ayvu_schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL);
INSERT INTO ayvu_schema_migrations(version, checksum)
VALUES (1, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
PRAGMA user_version=1;
