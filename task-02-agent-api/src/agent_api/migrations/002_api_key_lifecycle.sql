ALTER TABLE api_key_hashes
    ADD COLUMN scopes TEXT NOT NULL DEFAULT '[]' CHECK(length(scopes) <= 2048);

ALTER TABLE api_key_hashes
    ADD COLUMN rotated_from_key_id TEXT;

CREATE INDEX api_key_hashes_by_rotation
    ON api_key_hashes(tenant_id, rotated_from_key_id)
    WHERE rotated_from_key_id IS NOT NULL;
