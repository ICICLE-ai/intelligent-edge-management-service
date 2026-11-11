-- Turntable scan stations. An app's kind decides which page runs it:
-- 'inference' apps stream camera output, 'scanbot' apps take scan jobs.
-- A hardware generation's setup flow decides what the device wizard asks for:
-- 'camera' asks for cameras, 'scan-station' asks for the capture station.
ALTER TABLE model_cards ADD COLUMN IF NOT EXISTS app_kind TEXT NOT NULL DEFAULT 'inference';
ALTER TABLE device_generations ADD COLUMN IF NOT EXISTS setup_flow TEXT NOT NULL DEFAULT 'camera';

CREATE TABLE IF NOT EXISTS scan_jobs (
    device_uid            TEXT NOT NULL,
    job_id                TEXT NOT NULL,
    deployment_uid        TEXT,
    owner_tapis_username  TEXT NOT NULL,
    object_name           TEXT,
    state                 TEXT NOT NULL,
    job_json              TEXT NOT NULL,
    created_ts            DOUBLE PRECISION,
    updated_ts            DOUBLE PRECISION,
    reported_at           TEXT NOT NULL,
    PRIMARY KEY (device_uid, job_id),
    FOREIGN KEY (device_uid) REFERENCES devices(device_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_device_created
    ON scan_jobs(device_uid, created_ts DESC);

CREATE TABLE IF NOT EXISTS scan_service_status (
    device_uid      TEXT PRIMARY KEY,
    deployment_uid  TEXT,
    reachable       INTEGER NOT NULL,
    error           TEXT,
    reported_at     TEXT NOT NULL,
    FOREIGN KEY (device_uid) REFERENCES devices(device_uid) ON DELETE CASCADE
);
