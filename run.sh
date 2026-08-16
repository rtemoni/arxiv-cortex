#!/usr/bin/env bash

set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

export ARXIV_CORTEX_DATA_DIR="${ARXIV_CORTEX_DATA_DIR:-$project_dir/data}"
export HF_HOME="${HF_HOME:-$ARXIV_CORTEX_DATA_DIR/model-cache}"

launcher="$project_dir/.venv/bin/arxiv-cortex"
if [[ ! -x "$launcher" ]]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "Arxiv Cortex requires uv for first-time setup: https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  uv sync --frozen
fi

if (( $# > 0 )); then
  exec "$launcher" "$@"
fi

host="${ARXIV_CORTEX_HOST:-127.0.0.1}"
port="${ARXIV_CORTEX_PORT:-5000}"

exec "$launcher" --host "$host" --port "$port"
