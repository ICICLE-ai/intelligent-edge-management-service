-- Long-lived API keys issued to edge agents at enrollment (hashed at rest).
CREATE TABLE IF NOT EXISTS device_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_uid   TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    FOREIGN KEY (device_uid) REFERENCES devices(device_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_credentials_device ON device_credentials(device_uid);
CREATE INDEX IF NOT EXISTS idx_device_credentials_hash ON device_credentials(key_hash);
