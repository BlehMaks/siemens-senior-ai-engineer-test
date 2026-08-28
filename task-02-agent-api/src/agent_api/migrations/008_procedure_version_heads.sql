-- Preserve consumed version numbers without retaining erased procedure payloads.
CREATE TABLE IF NOT EXISTS procedure_version_heads (
    tenant_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    latest_version INTEGER NOT NULL CHECK(
        latest_version BETWEEN 1 AND 10000
    ),
    PRIMARY KEY (tenant_id, procedure_id)
) WITHOUT ROWID;

INSERT INTO procedure_version_heads (tenant_id, procedure_id, latest_version)
    SELECT tenant_id, procedure_id, MAX(version)
    FROM procedure_versions
    GROUP BY tenant_id, procedure_id
    ON CONFLICT (tenant_id, procedure_id) DO UPDATE SET
        latest_version = MAX(latest_version, excluded.latest_version);

INSERT OR IGNORE INTO tenants (tenant_id, display_name, created_at)
    SELECT DISTINCT tenant_id, NULL, '1970-01-01T00:00:00.000000+00:00'
    FROM procedure_version_heads;

CREATE TABLE procedure_version_heads_v1 (
    tenant_id TEXT NOT NULL,
    procedure_id TEXT NOT NULL,
    latest_version INTEGER NOT NULL CHECK(
        latest_version BETWEEN 1 AND 10000
    ),
    PRIMARY KEY (tenant_id, procedure_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
) WITHOUT ROWID;

INSERT INTO procedure_version_heads_v1
    (tenant_id, procedure_id, latest_version)
    SELECT tenant_id, procedure_id, latest_version
    FROM procedure_version_heads;

DROP TABLE procedure_version_heads;
ALTER TABLE procedure_version_heads_v1 RENAME TO procedure_version_heads;
