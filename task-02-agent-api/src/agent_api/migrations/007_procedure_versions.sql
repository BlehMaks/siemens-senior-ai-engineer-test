-- Task 1 can create both standalone shapes before the API owns the database.
CREATE TABLE IF NOT EXISTS procedure_versions (
    tenant_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    origin_session_id TEXT NOT NULL,
    origin_run_id TEXT NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 16384),
    PRIMARY KEY (tenant_id, procedure_id, version)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS active_procedures (
    tenant_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, procedure_id)
) WITHOUT ROWID;

INSERT OR IGNORE INTO tenants (tenant_id, display_name, created_at)
    SELECT DISTINCT tenant_id, NULL, '1970-01-01T00:00:00.000000+00:00'
    FROM procedure_versions;

INSERT OR IGNORE INTO sessions
    (tenant_id, session_id, label, created_at, updated_at)
    SELECT DISTINCT tenant_id, origin_session_id, NULL,
        '1970-01-01T00:00:00.000000+00:00',
        '1970-01-01T00:00:00.000000+00:00'
    FROM procedure_versions;

CREATE TABLE procedure_versions_v1 (
    tenant_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    origin_session_id TEXT NOT NULL,
    origin_run_id TEXT NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 16384),
    PRIMARY KEY (tenant_id, procedure_id, version),
    FOREIGN KEY (tenant_id, origin_session_id)
        REFERENCES sessions(tenant_id, session_id) ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO procedure_versions_v1
    (tenant_id, procedure_id, version, origin_session_id, origin_run_id,
     state, payload)
    SELECT tenant_id, procedure_id, version, origin_session_id, origin_run_id,
        state, payload FROM procedure_versions;

CREATE TABLE active_procedures_v1 (
    tenant_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, procedure_id),
    FOREIGN KEY (tenant_id, procedure_id, version)
        REFERENCES procedure_versions_v1(tenant_id, procedure_id, version)
        ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO active_procedures_v1 (tenant_id, procedure_id, version)
    SELECT tenant_id, procedure_id, version FROM active_procedures;

DROP TABLE active_procedures;
DROP TABLE procedure_versions;
ALTER TABLE procedure_versions_v1 RENAME TO procedure_versions;
ALTER TABLE active_procedures_v1 RENAME TO active_procedures;

CREATE INDEX procedure_versions_by_session
    ON procedure_versions(tenant_id, origin_session_id, procedure_id, version);
CREATE INDEX procedure_versions_by_review
    ON procedure_versions(tenant_id, state, procedure_id, version);
