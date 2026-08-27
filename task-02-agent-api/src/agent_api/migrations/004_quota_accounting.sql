CREATE TABLE quota_rate_buckets (
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    tokens REAL NOT NULL,
    last_refill TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key_id),
    FOREIGN KEY (tenant_id, key_id)
        REFERENCES api_key_hashes(tenant_id, key_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE quota_run_admissions (
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash BLOB NOT NULL CHECK(length(request_hash) = 32),
    run_id TEXT NOT NULL,
    work_day TEXT NOT NULL,
    work_units INTEGER NOT NULL CHECK(work_units >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX quota_run_admissions_by_day
    ON quota_run_admissions(tenant_id, work_day);

CREATE TABLE quota_execution_leases (
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    permit_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (permit_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES runs(tenant_id, run_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX quota_execution_leases_by_expiry
    ON quota_execution_leases(tenant_id, expires_at);

CREATE TABLE quota_sse_leases (
    tenant_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    permit_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (permit_id),
    FOREIGN KEY (tenant_id, key_id)
        REFERENCES api_key_hashes(tenant_id, key_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX quota_sse_leases_by_expiry
    ON quota_sse_leases(tenant_id, expires_at);
