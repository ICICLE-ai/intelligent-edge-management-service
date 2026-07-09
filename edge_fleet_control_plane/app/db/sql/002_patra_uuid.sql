-- ============================================================================
-- v2 researcher UX: Patra UUID as the natural key + cached raw docker command
-- ----------------------------------------------------------------------------
-- We keep `model_card_uid` as the surrogate PRIMARY KEY (every FK references
-- it). The Patra UUID becomes a first-class, UNIQUE field on `model_cards`
-- so the researcher can be confident there is exactly one card per Patra
-- model. NULL is allowed because we may also publish cards that do not yet
-- have a Patra entry.
-- ============================================================================

ALTER TABLE model_cards ADD COLUMN patra_model_card_uuid TEXT;
ALTER TABLE model_cards ADD COLUMN raw_docker_command    TEXT;

-- Backfill from the artifact table so existing seeded cards keep their UUID.
UPDATE model_cards
   SET patra_model_card_uuid = (
       SELECT a.patra_model_card_uuid
         FROM model_artifacts a
        WHERE a.model_card_uid = model_cards.model_card_uid
          AND a.patra_model_card_uuid IS NOT NULL
        LIMIT 1
   )
 WHERE patra_model_card_uuid IS NULL;

-- One model card per Patra UUID (NULLs ignored by partial index).
-- Removed in 003_app_publish.sql — multiple apps may share Patra weights.
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_model_cards_patra_uuid
--     ON model_cards(patra_model_card_uuid)
--     WHERE patra_model_card_uuid IS NOT NULL;
