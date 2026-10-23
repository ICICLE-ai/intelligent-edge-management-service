-- What the device actually started, recorded from the RUNNING ack, so the
-- portal can compare it with the immutable command payload.
ALTER TABLE device_deployments ADD COLUMN observed_artifact_sha256 TEXT;
ALTER TABLE device_deployments ADD COLUMN observed_artifact_bytes  INTEGER;
ALTER TABLE device_deployments ADD COLUMN observed_image_id        TEXT;
ALTER TABLE device_deployments ADD COLUMN observed_image_digest    TEXT;
ALTER TABLE device_deployments ADD COLUMN observed_image           TEXT;
ALTER TABLE device_deployments ADD COLUMN provenance_status        TEXT;
ALTER TABLE device_deployments ADD COLUMN provenance_detail        TEXT;
