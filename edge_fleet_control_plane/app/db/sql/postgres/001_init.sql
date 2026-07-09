-- ============================================================================
-- ICICLE Edge Control Plane — initial schema
-- ----------------------------------------------------------------------------
-- This file is loaded by app.db.migrations once. New schema changes go into
-- numbered follow-up files (002_…sql, 003_…sql, …) and are tracked in
-- schema_migrations.
-- ============================================================================

-- Identity ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    tapis_username  TEXT PRIMARY KEY,
    display_name    TEXT,
    email           TEXT,
    role            TEXT NOT NULL DEFAULT 'operator',  -- operator | researcher | admin
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Hardware catalog -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_generations (
    generation_uid     TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL,
    vendor             TEXT,
    device_family      TEXT,
    hardware_type      TEXT NOT NULL,
    architecture       TEXT NOT NULL,
    cuda_supported     INTEGER NOT NULL DEFAULT 1,
    default_runtime    TEXT,                          -- e.g. "nvidia"
    cpu_cores          INTEGER,
    memory_mb          INTEGER,
    storage_gb         INTEGER,
    description        TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_generations_active
    ON device_generations(is_active);

-- Fleet ------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_groups (
    group_uid             TEXT PRIMARY KEY,
    owner_tapis_username  TEXT NOT NULL,
    group_name            TEXT NOT NULL,
    description           TEXT,
    site_name             TEXT,
    color_tag             TEXT DEFAULT 'indigo',     -- UI accent
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE(owner_tapis_username, group_name),
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS devices (
    device_uid             TEXT PRIMARY KEY,
    owner_tapis_username   TEXT NOT NULL,
    device_name            TEXT NOT NULL,
    device_alias           TEXT,
    generation_uid         TEXT NOT NULL,
    group_uid              TEXT,
    site_name              TEXT,
    status                 TEXT NOT NULL DEFAULT 'REGISTERED_NOT_INSTALLED',
        -- REGISTERED_NOT_INSTALLED | INSTALLER_READY | ENROLLED |
        -- ONLINE | OFFLINE | RUNNING
    hostname               TEXT,
    ip_address             TEXT,
    agent_version          TEXT,
    last_heartbeat_at      TEXT,
    last_seen_at           TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    UNIQUE(owner_tapis_username, device_alias),
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username) ON DELETE CASCADE,
    FOREIGN KEY (generation_uid)       REFERENCES device_generations(generation_uid),
    FOREIGN KEY (group_uid)            REFERENCES device_groups(group_uid) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_owner  ON devices(owner_tapis_username);
CREATE INDEX IF NOT EXISTS idx_devices_group  ON devices(group_uid);
CREATE INDEX IF NOT EXISTS idx_devices_gen    ON devices(generation_uid);
CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status);

-- Enrollment / installer tokens -----------------------------------------------
CREATE TABLE IF NOT EXISTS device_enrollments (
    id                       BIGSERIAL PRIMARY KEY,
    device_uid               TEXT NOT NULL,
    owner_tapis_username     TEXT NOT NULL,
    token_hash               TEXT NOT NULL UNIQUE,
    expires_at               TEXT NOT NULL,
    used_at                  TEXT,
    installer_downloaded_at  TEXT,
    created_at               TEXT NOT NULL,
    FOREIGN KEY (device_uid)           REFERENCES devices(device_uid)        ON DELETE CASCADE,
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username)
);
CREATE INDEX IF NOT EXISTS idx_enrollments_device ON device_enrollments(device_uid);

-- Heartbeats with health-roll-up ----------------------------------------------
CREATE TABLE IF NOT EXISTS device_heartbeats (
    id                     BIGSERIAL PRIMARY KEY,
    device_uid             TEXT NOT NULL,
    owner_tapis_username   TEXT NOT NULL,
    status                 TEXT,
    cpu_percent            REAL,
    memory_used_mb         INTEGER,
    disk_used_gb           REAL,
    gpu_temp_c             REAL,
    docker_running         INTEGER,
    active_containers_json TEXT,        -- JSON array of {name,image,state}
    payload_json           TEXT,        -- raw payload for forensic use
    received_at            TEXT NOT NULL,
    FOREIGN KEY (device_uid)           REFERENCES devices(device_uid) ON DELETE CASCADE,
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username)
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_device_time
    ON device_heartbeats(device_uid, received_at DESC);

-- Model cards (researcher domain) ----------------------------------------------
CREATE TABLE IF NOT EXISTS model_cards (
    model_card_uid           TEXT PRIMARY KEY,
    owner_tapis_username     TEXT NOT NULL,
    slug                     TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    version                  TEXT NOT NULL,
    task_type                TEXT,                  -- e.g. "segmentation"
    framework                TEXT,                  -- e.g. "tensorrt"
    description              TEXT,
    license                  TEXT,
    homepage_url             TEXT,
    paper_url                TEXT,
    tags_json                TEXT,                  -- JSON array of strings
    status                   TEXT NOT NULL DEFAULT 'DRAFT',
        -- DRAFT | PUBLISHED | DEPRECATED
    published_at             TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    patra_model_card_uuid    TEXT,
    raw_docker_command       TEXT,
    app_id                   TEXT,
    visibility               TEXT NOT NULL DEFAULT 'private',
    UNIQUE(owner_tapis_username, slug, version),
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username)
);
CREATE INDEX IF NOT EXISTS idx_model_cards_owner  ON model_cards(owner_tapis_username);
CREATE INDEX IF NOT EXISTS idx_model_cards_status ON model_cards(status);

-- Where the model weights live (Patra UUID or direct URL) ---------------------
CREATE TABLE IF NOT EXISTS model_artifacts (
    artifact_uid             TEXT PRIMARY KEY,
    model_card_uid           TEXT NOT NULL,
    filename                 TEXT NOT NULL,           -- local file name on device
    container_path           TEXT NOT NULL,           -- inside-container path
    size_bytes               INTEGER,
    sha256                   TEXT,
    source_type              TEXT NOT NULL,           -- 'patra' | 'url'
    patra_model_card_uuid    TEXT,                    -- when source_type='patra'
    download_url             TEXT,                    -- when source_type='url' (resolved at deploy time)
    content_type             TEXT,
    notes                    TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY (model_card_uid) REFERENCES model_cards(model_card_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_model_artifacts_card ON model_artifacts(model_card_uid);

-- Container execution spec for a model ----------------------------------------
CREATE TABLE IF NOT EXISTS container_specs (
    spec_uid                 TEXT PRIMARY KEY,
    model_card_uid           TEXT NOT NULL UNIQUE,
    image_registry           TEXT,                    -- optional, defaults to docker.io
    image_repository         TEXT NOT NULL,
    image_tag                TEXT NOT NULL DEFAULT 'latest',
    image_digest             TEXT,
    container_name           TEXT NOT NULL,
    pull_policy              TEXT NOT NULL DEFAULT 'if_not_present',  -- always | if_not_present | never
    remove_after_exit        INTEGER NOT NULL DEFAULT 0,
    restart_policy           TEXT DEFAULT 'no',
    entrypoint_json          TEXT,                    -- JSON array
    command_json             TEXT,                    -- JSON array (CMD override)
    working_dir              TEXT,
    model_env_var            TEXT,                    -- name of env var receiving the model path
    network_mode             TEXT,                    -- host | bridge | none | custom
    gpus                     TEXT,                    -- "all" | "device=..." | NULL
    runtime                  TEXT,                    -- "nvidia"
    privileged               INTEGER NOT NULL DEFAULT 0,
    ipc_mode                 TEXT,                    -- host | shareable | container:<id>
    shm_size                 TEXT,                    -- e.g. "1g"
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY (model_card_uid) REFERENCES model_cards(model_card_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS container_spec_env (
    id                       BIGSERIAL PRIMARY KEY,
    spec_uid                 TEXT NOT NULL,
    var_key                  TEXT NOT NULL,
    var_value                TEXT NOT NULL,
    is_secret                INTEGER NOT NULL DEFAULT 0,
    sort_order               INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (spec_uid) REFERENCES container_specs(spec_uid) ON DELETE CASCADE,
    UNIQUE(spec_uid, var_key)
);

CREATE TABLE IF NOT EXISTS container_spec_mounts (
    id                       BIGSERIAL PRIMARY KEY,
    spec_uid                 TEXT NOT NULL,
    source                   TEXT NOT NULL,           -- may contain ${DEPLOYMENT_DIR} / ${MODEL_FILE}
    target                   TEXT NOT NULL,
    mount_style              TEXT NOT NULL DEFAULT 'volume',   -- mount | volume (-v)
    mount_type               TEXT NOT NULL DEFAULT 'bind',     -- bind | volume | tmpfs
    mode                     TEXT,                    -- ro | rw | NULL
    sort_order               INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (spec_uid) REFERENCES container_specs(spec_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS container_spec_docker_args (
    id                       BIGSERIAL PRIMARY KEY,
    spec_uid                 TEXT NOT NULL,
    arg                      TEXT NOT NULL,           -- single CLI arg ("--network", "host", ...)
    sort_order               INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (spec_uid) REFERENCES container_specs(spec_uid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS container_spec_ports (
    id                       BIGSERIAL PRIMARY KEY,
    spec_uid                 TEXT NOT NULL,
    host_port                INTEGER,                 -- NULL means publish-all/random
    container_port           INTEGER NOT NULL,
    protocol                 TEXT NOT NULL DEFAULT 'tcp',
    sort_order               INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (spec_uid) REFERENCES container_specs(spec_uid) ON DELETE CASCADE
);

-- Many-to-many compatibility (which devices can run which model) --------------
CREATE TABLE IF NOT EXISTS model_compatibility (
    id                       BIGSERIAL PRIMARY KEY,
    model_card_uid           TEXT NOT NULL,
    generation_uid           TEXT NOT NULL,
    min_memory_mb            INTEGER,
    min_storage_gb           INTEGER,
    requires_cuda            INTEGER NOT NULL DEFAULT 1,
    notes                    TEXT,
    created_at               TEXT NOT NULL,
    UNIQUE(model_card_uid, generation_uid),
    FOREIGN KEY (model_card_uid) REFERENCES model_cards(model_card_uid) ON DELETE CASCADE,
    FOREIGN KEY (generation_uid) REFERENCES device_generations(generation_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_compat_gen ON model_compatibility(generation_uid);

-- Deployments (one logical "run this model on this target") -------------------
CREATE TABLE IF NOT EXISTS deployments (
    deployment_uid          TEXT PRIMARY KEY,
    owner_tapis_username    TEXT NOT NULL,
    model_card_uid          TEXT NOT NULL,
    artifact_uid            TEXT NOT NULL,
    spec_uid                TEXT NOT NULL,
    target_type             TEXT NOT NULL,        -- DEVICE | GROUP
    target_uid              TEXT NOT NULL,
    target_name             TEXT,                 -- denormalised label for fast UI
    status                  TEXT NOT NULL DEFAULT 'PENDING',
        -- PENDING | DELIVERING | RUNNING | DEGRADED | STOPPING | STOPPED | FAILED
    request_id              TEXT NOT NULL UNIQUE,
    notes                   TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    started_at              TEXT,
    stopped_at              TEXT,
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username),
    FOREIGN KEY (model_card_uid) REFERENCES model_cards(model_card_uid),
    FOREIGN KEY (artifact_uid)   REFERENCES model_artifacts(artifact_uid),
    FOREIGN KEY (spec_uid)       REFERENCES container_specs(spec_uid)
);
CREATE INDEX IF NOT EXISTS idx_deployments_owner   ON deployments(owner_tapis_username);
CREATE INDEX IF NOT EXISTS idx_deployments_status  ON deployments(status);
CREATE INDEX IF NOT EXISTS idx_deployments_target  ON deployments(target_type, target_uid);

-- Per-device materialisation of a deployment (group → many devices) -----------
CREATE TABLE IF NOT EXISTS device_deployments (
    device_deployment_uid   TEXT PRIMARY KEY,
    deployment_uid          TEXT NOT NULL,
    device_uid              TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'PENDING',
        -- PENDING | SENT | DOWNLOADING | PULLING | STARTING | RUNNING |
        -- STOPPING | STOPPED | FAILED
    container_id            TEXT,
    container_name          TEXT,
    error_message           TEXT,
    started_at              TEXT,
    stopped_at              TEXT,
    last_status_at          TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE(deployment_uid, device_uid),
    FOREIGN KEY (deployment_uid) REFERENCES deployments(deployment_uid) ON DELETE CASCADE,
    FOREIGN KEY (device_uid)     REFERENCES devices(device_uid)         ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_devdep_device ON device_deployments(device_uid);
CREATE INDEX IF NOT EXISTS idx_devdep_dep    ON device_deployments(deployment_uid);

-- Outgoing commands (every MQTT publish is recorded here) ---------------------
CREATE TABLE IF NOT EXISTS device_commands (
    command_uid             TEXT PRIMARY KEY,
    owner_tapis_username    TEXT NOT NULL,
    deployment_uid          TEXT,
    target_type             TEXT NOT NULL,        -- DEVICE | GROUP
    target_uid              TEXT NOT NULL,
    device_uid              TEXT,                 -- NULL for GROUP-broadcast
    operation               TEXT NOT NULL,
        -- deploy_model | stop_deployment | restart_container | status | logs
    request_id              TEXT NOT NULL UNIQUE,
    status                  TEXT NOT NULL DEFAULT 'RECORDED',
        -- RECORDED | MQTT_SENT | MQTT_FAILED | ACK | FAILED
    topic                   TEXT NOT NULL,
    payload_json            TEXT NOT NULL,
    response_json           TEXT,
    error_message           TEXT,
    created_at              TEXT NOT NULL,
    sent_at                 TEXT,
    acked_at                TEXT,
    FOREIGN KEY (owner_tapis_username) REFERENCES users(tapis_username),
    FOREIGN KEY (deployment_uid)       REFERENCES deployments(deployment_uid) ON DELETE SET NULL,
    FOREIGN KEY (device_uid)           REFERENCES devices(device_uid)         ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_dep    ON device_commands(deployment_uid);
CREATE INDEX IF NOT EXISTS idx_commands_device ON device_commands(device_uid);
CREATE INDEX IF NOT EXISTS idx_commands_status ON device_commands(status);
CREATE INDEX IF NOT EXISTS idx_commands_time   ON device_commands(created_at DESC);

-- General event log (typed, queryable, fed by every service) ------------------
CREATE TABLE IF NOT EXISTS events (
    id                       BIGSERIAL PRIMARY KEY,
    owner_tapis_username     TEXT,
    device_uid               TEXT,
    deployment_uid           TEXT,
    event_type               TEXT NOT NULL,
    severity                 TEXT NOT NULL DEFAULT 'INFO',  -- DEBUG | INFO | WARN | ERROR
    message                  TEXT,
    payload_json             TEXT,
    created_at               TEXT NOT NULL,
    FOREIGN KEY (device_uid)     REFERENCES devices(device_uid)         ON DELETE SET NULL,
    FOREIGN KEY (deployment_uid) REFERENCES deployments(deployment_uid) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_events_owner_time ON events(owner_tapis_username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_device     ON events(device_uid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_dep        ON events(deployment_uid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(event_type);

-- Raw MQTT audit (inbound + outbound) -----------------------------------------
CREATE TABLE IF NOT EXISTS mqtt_audit (
    id                       BIGSERIAL PRIMARY KEY,
    owner_tapis_username     TEXT,
    topic                    TEXT NOT NULL,
    direction                TEXT NOT NULL,         -- OUTBOUND | INBOUND
    request_id               TEXT,
    device_uid               TEXT,
    payload_json             TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    FOREIGN KEY (device_uid) REFERENCES devices(device_uid) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_mqtt_audit_time   ON mqtt_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mqtt_audit_device ON mqtt_audit(device_uid, created_at DESC);


CREATE UNIQUE INDEX IF NOT EXISTS idx_model_cards_app_id
    ON model_cards(app_id);

CREATE INDEX IF NOT EXISTS idx_model_cards_visibility
    ON model_cards(visibility);

CREATE TABLE IF NOT EXISTS device_credentials (
    id           BIGSERIAL PRIMARY KEY,
    device_uid   TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT,
    FOREIGN KEY (device_uid) REFERENCES devices(device_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_device_credentials_device ON device_credentials(device_uid);
CREATE INDEX IF NOT EXISTS idx_device_credentials_hash ON device_credentials(key_hash);
