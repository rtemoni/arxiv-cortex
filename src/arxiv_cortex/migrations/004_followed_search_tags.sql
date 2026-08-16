ALTER TABLE search_tags ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0
    CHECK (enabled IN (0, 1));
ALTER TABLE search_tags ADD COLUMN backfill_from TEXT;
ALTER TABLE search_tags ADD COLUMN backfill_complete INTEGER NOT NULL DEFAULT 0
    CHECK (backfill_complete IN (0, 1));
ALTER TABLE search_tags ADD COLUMN last_updated_watermark TEXT;
ALTER TABLE search_tags ADD COLUMN last_synced_at TEXT;

UPDATE search_tags
SET backfill_from = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-90 days')
WHERE backfill_from IS NULL;

CREATE INDEX IF NOT EXISTS idx_search_tags_enabled ON search_tags(enabled, name);

CREATE TABLE IF NOT EXISTS paper_search_tags (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES search_tags(id) ON DELETE CASCADE,
    PRIMARY KEY (paper_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_search_tags_tag
ON paper_search_tags(tag_id, paper_id);
