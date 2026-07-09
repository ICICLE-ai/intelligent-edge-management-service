-- ============================================================================
-- App publish: auto app_id, public/private visibility, drop Patra uniqueness
-- ----------------------------------------------------------------------------
-- Multiple edge apps may reference the same Patra model weights with different
-- container images. app_id is system-assigned and globally unique.
-- ============================================================================

ALTER TABLE model_cards ADD COLUMN app_id TEXT;
ALTER TABLE model_cards ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';

UPDATE model_cards
   SET app_id = 'app_' || substr(model_card_uid, 4)
 WHERE app_id IS NULL;

UPDATE model_cards
   SET visibility = 'public'
 WHERE owner_tapis_username = 'system' AND status = 'PUBLISHED';

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_cards_app_id
    ON model_cards(app_id);

CREATE INDEX IF NOT EXISTS idx_model_cards_visibility
    ON model_cards(visibility);

DROP INDEX IF EXISTS idx_model_cards_patra_uuid;
