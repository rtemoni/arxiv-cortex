ALTER TABLE papers ADD COLUMN citation_count INTEGER
    CHECK (citation_count IS NULL OR citation_count >= 0);
ALTER TABLE papers ADD COLUMN citation_updated_at TEXT;
ALTER TABLE papers ADD COLUMN semantic_scholar_id TEXT;

CREATE INDEX IF NOT EXISTS idx_papers_citations
ON papers(citation_count DESC)
WHERE citation_count IS NOT NULL;

CREATE VIRTUAL TABLE paper_substring_fts USING fts5(
    title,
    authors,
    abstract,
    content='papers',
    content_rowid='id',
    tokenize='trigram'
);

INSERT INTO paper_substring_fts(rowid, title, authors, abstract)
SELECT id, title, authors_text, abstract FROM papers;

CREATE TRIGGER papers_substring_fts_insert AFTER INSERT ON papers BEGIN
    INSERT INTO paper_substring_fts(rowid, title, authors, abstract)
    VALUES (new.id, new.title, new.authors_text, new.abstract);
END;

CREATE TRIGGER papers_substring_fts_delete AFTER DELETE ON papers BEGIN
    INSERT INTO paper_substring_fts(paper_substring_fts, rowid, title, authors, abstract)
    VALUES ('delete', old.id, old.title, old.authors_text, old.abstract);
END;

CREATE TRIGGER papers_substring_fts_update
AFTER UPDATE OF title, authors_text, abstract ON papers BEGIN
    INSERT INTO paper_substring_fts(paper_substring_fts, rowid, title, authors, abstract)
    VALUES ('delete', old.id, old.title, old.authors_text, old.abstract);
    INSERT INTO paper_substring_fts(rowid, title, authors, abstract)
    VALUES (new.id, new.title, new.authors_text, new.abstract);
END;
