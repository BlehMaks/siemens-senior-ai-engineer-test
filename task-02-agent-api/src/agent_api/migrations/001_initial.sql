CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE api_key_hashes (
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    key_hash BLOB NOT NULL CHECK(length(key_hash) BETWEEN 32 AND 128),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    PRIMARY KEY (tenant_id, key_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE sessions (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, session_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE runs (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    state TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 262144),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions(tenant_id, session_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX runs_by_session
    ON runs(tenant_id, session_id, created_at, run_id);

CREATE TABLE idempotency_records (
    tenant_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash BLOB NOT NULL CHECK(length(request_hash) = 32),
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES runs(tenant_id, run_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE run_events (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 262144),
    PRIMARY KEY (tenant_id, run_id, sequence),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES runs(tenant_id, run_id) ON DELETE CASCADE
) WITHOUT ROWID;

-- Task 1 may already own this exact table. Build a compatible staging shape,
-- import its tenants, then rebuild it with API-level deletion enforcement.
CREATE TABLE IF NOT EXISTS run_reflections (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 65536),
    PRIMARY KEY (tenant_id, session_id, run_id)
) WITHOUT ROWID;

INSERT OR IGNORE INTO tenants (tenant_id, display_name, created_at)
    SELECT DISTINCT tenant_id, NULL, '1970-01-01T00:00:00.000000+00:00'
    FROM run_reflections;

CREATE TABLE run_reflections_v1 (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 65536),
    PRIMARY KEY (tenant_id, session_id, run_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO run_reflections_v1 (tenant_id, session_id, run_id, payload)
    SELECT tenant_id, session_id, run_id, payload FROM run_reflections;

DROP TABLE run_reflections;

ALTER TABLE run_reflections_v1 RENAME TO run_reflections;

CREATE TABLE audit_entries (
    tenant_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    action TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, entry_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX audit_entries_by_time
    ON audit_entries(tenant_id, occurred_at, entry_id);
