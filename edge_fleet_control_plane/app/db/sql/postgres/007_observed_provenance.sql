ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS observed_artifact_sha256 TEXT;
ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS observed_artifact_bytes  INTEGER;
ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS observed_image_id        TEXT;
ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS observed_image_digest    TEXT;
ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS observed_image           TEXT;
ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS provenance_status        TEXT;
ALTER TABLE device_deployments ADD COLUMN IF NOT EXISTS provenance_detail        TEXT;
