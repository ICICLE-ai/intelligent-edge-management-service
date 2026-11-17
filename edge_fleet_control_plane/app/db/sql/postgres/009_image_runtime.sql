-- The runtime an engine is loaded in belongs to the container image, not to
-- the engine: the image is built for an L4T release, and it either ships its
-- own TensorRT or uses the one the device provides ('host').
-- engine_l4t_version is kept on model_artifacts as a legacy field; admission
-- prefers image_l4t_version when both are set.
ALTER TABLE container_specs ADD COLUMN IF NOT EXISTS image_tensorrt_version TEXT;  -- prefix such as "10.9", or 'host'
ALTER TABLE container_specs ADD COLUMN IF NOT EXISTS image_l4t_version      TEXT;  -- prefix such as "36.4" or "32"

UPDATE container_specs
   SET image_l4t_version = (
       SELECT a.engine_l4t_version FROM model_artifacts a
        WHERE a.model_card_uid = container_specs.model_card_uid
          AND a.engine_l4t_version IS NOT NULL
        LIMIT 1)
 WHERE image_l4t_version IS NULL;

UPDATE container_specs
   SET image_tensorrt_version = 'host'
 WHERE image_tensorrt_version IS NULL
   AND EXISTS (
       SELECT 1 FROM model_artifacts a
        WHERE a.model_card_uid = container_specs.model_card_uid
          AND a.engine_tensorrt_version IS NOT NULL);
