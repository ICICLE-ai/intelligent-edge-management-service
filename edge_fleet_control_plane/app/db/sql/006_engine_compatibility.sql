-- Build target of a compiled engine (TensorRT engines only load on the GPU
-- architecture and TensorRT release they were built with), and the platform
-- each device reports about itself, so deployments can be checked against both.
ALTER TABLE model_artifacts ADD COLUMN engine_compute_capability TEXT;  -- e.g. "8.7"
ALTER TABLE model_artifacts ADD COLUMN engine_tensorrt_version   TEXT;  -- prefix, e.g. "7" or "8.5.2"
ALTER TABLE model_artifacts ADD COLUMN engine_l4t_version        TEXT;  -- prefix, e.g. "32" or "35.4"
ALTER TABLE model_artifacts ADD COLUMN engine_precision          TEXT;  -- fp32 | fp16 | int8

ALTER TABLE devices ADD COLUMN platform_json        TEXT;  -- reported by agent v2.2.0+
ALTER TABLE devices ADD COLUMN platform_reported_at TEXT;
