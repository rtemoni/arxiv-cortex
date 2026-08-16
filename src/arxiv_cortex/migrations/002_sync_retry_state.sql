ALTER TABLE sync_runs ADD COLUMN current_category TEXT;
ALTER TABLE sync_runs ADD COLUMN retry_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN retry_status INTEGER;
ALTER TABLE sync_runs ADD COLUMN retry_reason TEXT;
ALTER TABLE sync_runs ADD COLUMN next_attempt_at TEXT;
