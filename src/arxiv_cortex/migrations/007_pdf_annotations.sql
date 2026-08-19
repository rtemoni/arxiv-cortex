CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    revision_label TEXT NOT NULL,
    source_checksum TEXT,
    pdf_fingerprint TEXT,
    artifact_path TEXT,
    byte_size INTEGER,
    page_count INTEGER,
    status TEXT NOT NULL CHECK (status IN ('ready', 'failed')),
    error TEXT,
    stale INTEGER NOT NULL DEFAULT 0 CHECK (stale IN (0, 1)),
    created_at TEXT NOT NULL,
    fetched_at TEXT,
    last_opened_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_paper
ON documents(paper_id, stale, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_documents_checksum
ON documents(paper_id, source_checksum)
WHERE source_checksum IS NOT NULL;

CREATE TABLE IF NOT EXISTS paper_highlights (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    quote TEXT NOT NULL CHECK (length(trim(quote)) > 0),
    note TEXT NOT NULL DEFAULT '',
    client_request_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_highlights_paper
ON paper_highlights(paper_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_highlights_document
ON paper_highlights(document_id, created_at);

CREATE TABLE IF NOT EXISTS paper_highlight_fragments (
    id INTEGER PRIMARY KEY,
    highlight_id INTEGER NOT NULL REFERENCES paper_highlights(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    page_rotation INTEGER NOT NULL DEFAULT 0,
    quads_json TEXT NOT NULL,
    UNIQUE (highlight_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_highlight_fragments_page
ON paper_highlight_fragments(highlight_id, page_number, ordinal);

CREATE TABLE IF NOT EXISTS paper_notes (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    body TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS paper_highlight_fts USING fts5(
    quote,
    note,
    content='paper_highlights',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS paper_highlight_fts_insert
AFTER INSERT ON paper_highlights BEGIN
    INSERT INTO paper_highlight_fts(rowid, quote, note)
    VALUES (new.id, new.quote, new.note);
END;

CREATE TRIGGER IF NOT EXISTS paper_highlight_fts_delete
AFTER DELETE ON paper_highlights BEGIN
    INSERT INTO paper_highlight_fts(paper_highlight_fts, rowid, quote, note)
    VALUES ('delete', old.id, old.quote, old.note);
END;

CREATE TRIGGER IF NOT EXISTS paper_highlight_fts_update
AFTER UPDATE OF quote, note ON paper_highlights BEGIN
    INSERT INTO paper_highlight_fts(paper_highlight_fts, rowid, quote, note)
    VALUES ('delete', old.id, old.quote, old.note);
    INSERT INTO paper_highlight_fts(rowid, quote, note)
    VALUES (new.id, new.quote, new.note);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS paper_note_fts USING fts5(
    body,
    content='paper_notes',
    content_rowid='paper_id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS paper_note_fts_insert
AFTER INSERT ON paper_notes BEGIN
    INSERT INTO paper_note_fts(rowid, body) VALUES (new.paper_id, new.body);
END;

CREATE TRIGGER IF NOT EXISTS paper_note_fts_delete
AFTER DELETE ON paper_notes BEGIN
    INSERT INTO paper_note_fts(paper_note_fts, rowid, body)
    VALUES ('delete', old.paper_id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS paper_note_fts_update
AFTER UPDATE OF body ON paper_notes BEGIN
    INSERT INTO paper_note_fts(paper_note_fts, rowid, body)
    VALUES ('delete', old.paper_id, old.body);
    INSERT INTO paper_note_fts(rowid, body) VALUES (new.paper_id, new.body);
END;
