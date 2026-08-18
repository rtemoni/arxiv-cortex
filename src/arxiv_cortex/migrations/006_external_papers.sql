ALTER TABLE papers ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'arxiv';
ALTER TABLE papers ADD COLUMN source_identifier TEXT;
ALTER TABLE papers ADD COLUMN source_name TEXT NOT NULL DEFAULT 'arXiv';
ALTER TABLE papers ADD COLUMN venue TEXT;

UPDATE papers
SET source_identifier = arxiv_id
WHERE source_identifier IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_source_identifier
ON papers(source_kind, source_identifier)
WHERE source_identifier IS NOT NULL;
