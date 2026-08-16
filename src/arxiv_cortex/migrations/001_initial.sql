CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    arxiv_id TEXT NOT NULL UNIQUE,
    latest_version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    authors_text TEXT NOT NULL,
    primary_category TEXT NOT NULL,
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    doi TEXT,
    journal_ref TEXT,
    comment TEXT,
    license_url TEXT,
    abstract_url TEXT NOT NULL,
    pdf_url TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_papers_updated ON papers(updated_at DESC);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (paper_id, position)
);

CREATE INDEX IF NOT EXISTS idx_paper_authors_name ON paper_authors(name);

CREATE TABLE IF NOT EXISTS paper_categories (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (paper_id, category)
);

CREATE INDEX IF NOT EXISTS idx_paper_categories_category ON paper_categories(category, paper_id);

CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
    title,
    authors,
    abstract,
    content='papers',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS papers_fts_insert AFTER INSERT ON papers BEGIN
    INSERT INTO paper_fts(rowid, title, authors, abstract)
    VALUES (new.id, new.title, new.authors_text, new.abstract);
END;

CREATE TRIGGER IF NOT EXISTS papers_fts_delete AFTER DELETE ON papers BEGIN
    INSERT INTO paper_fts(paper_fts, rowid, title, authors, abstract)
    VALUES ('delete', old.id, old.title, old.authors_text, old.abstract);
END;

CREATE TRIGGER IF NOT EXISTS papers_fts_update AFTER UPDATE OF title, authors_text, abstract ON papers BEGIN
    INSERT INTO paper_fts(paper_fts, rowid, title, authors, abstract)
    VALUES ('delete', old.id, old.title, old.authors_text, old.abstract);
    INSERT INTO paper_fts(rowid, title, authors, abstract)
    VALUES (new.id, new.title, new.authors_text, new.abstract);
END;

CREATE TABLE IF NOT EXISTS paper_embeddings (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON paper_embeddings(model_id, paper_id);

CREATE TABLE IF NOT EXISTS paper_state (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    saved_at TEXT,
    read_at TEXT,
    dismissed_at TEXT,
    last_opened_at TEXT,
    CHECK (saved_at IS NULL OR dismissed_at IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_state_saved ON paper_state(saved_at) WHERE saved_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_state_read ON paper_state(read_at) WHERE read_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_state_dismissed ON paper_state(dismissed_at) WHERE dismissed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS feed_subscriptions (
    category TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    backfill_from TEXT NOT NULL,
    backfill_complete INTEGER NOT NULL DEFAULT 0 CHECK (backfill_complete IN (0, 1)),
    last_updated_watermark TEXT,
    created_at TEXT NOT NULL,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    trigger TEXT NOT NULL,
    categories_total INTEGER NOT NULL DEFAULT 0,
    categories_done INTEGER NOT NULL DEFAULT 0,
    papers_seen INTEGER NOT NULL DEFAULT 0,
    papers_added INTEGER NOT NULL DEFAULT 0,
    papers_updated INTEGER NOT NULL DEFAULT 0,
    embeddings_generated INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_created ON sync_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

INSERT OR IGNORE INTO settings(key, value, updated_at)
VALUES
    ('embedding_model', 'sentence-transformers/all-MiniLM-L6-v2', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('sync_time', '06:00', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('recommendation_days', '30', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ('scheduler_enabled', 'true', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
