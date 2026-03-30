#!/usr/bin/env bash
# Quick report on all_comparisons.db — just run it.
#
# Usage:
#   bash utils/report.sh                          # report from ./all_comparisons.db
#   bash utils/report.sh /path/to/other.db        # custom DB path
#   bash utils/report.sh --latex                   # include LaTeX snippets
#   bash utils/report.sh --cross-only              # only cross comparisons
#   bash utils/report.sh --conv-only               # only convergence

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${1:-all_comparisons.db}"

# If first arg is a flag, use default DB
if [[ "$DB" == --* ]]; then
    DB="all_comparisons.db"
    EXTRA_ARGS=("$@")
else
    shift || true
    EXTRA_ARGS=("$@")
fi

# Find Python
if [[ -f "${SCRIPT_DIR}/../.venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/../.venv/bin/python"
elif [[ -f "${SCRIPT_DIR}/../../.venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/../../.venv/bin/python"
else
    PYTHON="python3"
fi

if [[ ! -f "$DB" ]]; then
    echo "DB not found: $DB"
    echo "Usage: bash utils/report.sh [path/to/all_comparisons.db] [--latex] [--cross-only] [--conv-only]"
    exit 1
fi

exec "$PYTHON" "${SCRIPT_DIR}/report_serde.py" "$DB" "${EXTRA_ARGS[@]}"
