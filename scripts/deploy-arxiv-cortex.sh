#!/usr/bin/env bash

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
database="$project_dir/data/arxiv-cortex.sqlite3"
backup_dir="$project_dir/data/backups"
before_tag_count=""

cd "$project_dir"

if [[ -f "$database" ]]; then
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 is required to create a safe pre-deploy backup." >&2
    exit 1
  fi

  mkdir -p "$backup_dir"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="$backup_dir/arxiv-cortex-before-deploy-$timestamp.sqlite3"
  backup_uri="file:$backup?immutable=1"
  sqlite3 "$database" ".backup '$backup'"
  integrity="$(sqlite3 "$backup_uri" "PRAGMA integrity_check;")"
  if [[ "$integrity" != "ok" ]]; then
    echo "Pre-deploy backup failed its integrity check: $integrity" >&2
    exit 1
  fi

  if [[ "$(sqlite3 "$backup_uri" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='search_tags';")" == "1" ]]; then
    before_tag_count="$(sqlite3 "$backup_uri" "SELECT COUNT(*) FROM search_tags;")"
  fi
  echo "Created verified backup: $backup"
fi

docker compose config --quiet
docker compose up -d --build --force-recreate --remove-orphans --wait

if [[ -n "$before_tag_count" ]]; then
  after_tag_count="$(sqlite3 "$database" "SELECT COUNT(*) FROM search_tags;")"
  if [[ "$after_tag_count" != "$before_tag_count" ]]; then
    echo "Research tag count changed during deploy ($before_tag_count -> $after_tag_count)." >&2
    exit 1
  fi
fi

health="$(curl --fail --silent --show-error http://127.0.0.1:5000/api/v1/health)"
if [[ "$health" != *'"research_tags"'* || "$health" != *'"tag_subscriptions"'* ]]; then
  echo "The deployed health response does not include research-tag support." >&2
  exit 1
fi

echo "Arxiv Cortex is healthy and research tags were preserved."
