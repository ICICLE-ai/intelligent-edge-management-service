-- Build target of a compiled engine (TensorRT engines only load on the GPU
-- architecture and TensorRT release they were built with), and the platform
-- each device reports about itself, so deployments can be checked against both.
ALTER TABLE model_artifacts ADD COLUMN IF NOT EXISTS engine_compute_capability TEXT;
ALTER TABLE model_artifacts ADD COLUMN IF NOT EXISTS engine_tensorrt_version   TEXT;
ALTER TABLE model_artifacts ADD COLUMN IF NOT EXISTS engine_l4t_version        TEXT;
ALTER TABLE model_artifacts ADD COLUMN IF NOT EXISTS engine_precision          TEXT;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS platform_json        TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS platform_reported_at TEXT;
