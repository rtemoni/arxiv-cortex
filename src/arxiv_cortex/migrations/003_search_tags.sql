CREATE TABLE IF NOT EXISTS search_tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(name)) BETWEEN 1 AND 60),
    CHECK (length(description) <= 240),
    CHECK (length(trim(keywords)) > 0)
);

INSERT OR IGNORE INTO search_tags(name, description, keywords, created_at, updated_at)
VALUES (
    'Hardware Verification',
    'Correctness, fault tolerance, and formal assurance for hardware used to train and run AI systems.',
    'hardware verification
AI accelerator verification
neural accelerator verification
inference accelerator reliability
training accelerator reliability
AI hardware safety
machine learning hardware fault
accelerator fault injection
silent data corruption AI
numerical correctness inference
numerical correctness training
formal verification accelerator
GPU verification machine learning
TPU verification',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);
