-- Per-device hardware capabilities (camera buses, count, host mounts).
ALTER TABLE devices ADD COLUMN capabilities_json TEXT;
