-- Task 1 can create the standalone shape before the API owns the database.
CREATE TABLE IF NOT EXISTS semantic_facts (
    tenant_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    origin_session_id TEXT NOT NULL,
    origin_run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    conflict_key TEXT NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 16384),
    PRIMARY KEY (tenant_id, fact_id)
) WITHOUT ROWID;

INSERT OR IGNORE INTO tenants (tenant_id, display_name, created_at)
    SELECT DISTINCT tenant_id, NULL, '1970-01-01T00:00:00.000000+00:00'
    FROM semantic_facts;

INSERT OR IGNORE INTO sessions
    (tenant_id, session_id, label, created_at, updated_at)
    SELECT DISTINCT tenant_id, origin_session_id, NULL,
        '1970-01-01T00:00:00.000000+00:00',
        '1970-01-01T00:00:00.000000+00:00'
    FROM semantic_facts;

CREATE TABLE semantic_facts_v1 (
    tenant_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    origin_session_id TEXT NOT NULL,
    origin_run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    conflict_key TEXT NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(length(payload) <= 16384),
    PRIMARY KEY (tenant_id, fact_id),
    FOREIGN KEY (tenant_id, origin_session_id)
        REFERENCES sessions(tenant_id, session_id) ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO semantic_facts_v1
    (tenant_id, fact_id, origin_session_id, origin_run_id, source_id,
     conflict_key, state, expires_at, payload)
    SELECT tenant_id, fact_id, origin_session_id, origin_run_id, source_id,
        conflict_key, state, expires_at, payload
    FROM semantic_facts;

DROP TABLE semantic_facts;
ALTER TABLE semantic_facts_v1 RENAME TO semantic_facts;

CREATE INDEX semantic_facts_by_review
    ON semantic_facts(tenant_id, state, conflict_key, fact_id);
CREATE INDEX semantic_facts_by_source
    ON semantic_facts(tenant_id, source_id, fact_id);
CREATE INDEX semantic_facts_by_session
    ON semantic_facts(tenant_id, origin_session_id, fact_id);
