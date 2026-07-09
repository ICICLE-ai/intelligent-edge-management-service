-- Per-device hardware capabilities (camera buses, count, host mounts).
ALTER TABLE devices ADD COLUMN IF NOT EXISTS capabilities_json TEXT;
