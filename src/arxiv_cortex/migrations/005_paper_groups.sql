CREATE TABLE IF NOT EXISTS paper_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(name)) BETWEEN 1 AND 60)
);

CREATE TABLE IF NOT EXISTS paper_group_memberships (
    group_id INTEGER NOT NULL REFERENCES paper_groups(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (group_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_group_memberships_paper
ON paper_group_memberships(paper_id, group_id);
