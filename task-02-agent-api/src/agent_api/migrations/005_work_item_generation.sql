ALTER TABLE work_items ADD COLUMN generation_id TEXT;

UPDATE runs
SET payload = json_set(
    payload,
    '$.generation_id',
    'generation-' || lower(hex(randomblob(16)))
)
WHERE json_type(payload, '$.generation_id') IS NULL;

UPDATE work_items
SET generation_id = (
    SELECT json_extract(runs.payload, '$.generation_id')
    FROM runs
    WHERE runs.tenant_id = work_items.tenant_id
      AND runs.run_id = work_items.run_id
);
