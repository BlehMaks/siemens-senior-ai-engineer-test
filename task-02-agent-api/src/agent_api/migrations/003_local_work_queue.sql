CREATE TABLE work_items (
    work_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    not_before TEXT NOT NULL,
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES runs(tenant_id, run_id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX work_items_by_due
    ON work_items(not_before, enqueued_at, work_id);

CREATE INDEX work_items_by_run
    ON work_items(tenant_id, run_id, work_id);
