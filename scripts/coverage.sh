#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_PATH="${HERMES_COVERAGE_JSON:-$ROOT/coverage.json}"

if [[ $# -eq 0 ]]; then
  set -- tests/ -v --timeout=60
fi

exec "$ROOT/scripts/test.sh" \
  --cov \
  --cov-config="$ROOT/pyproject.toml" \
  --cov-report=term-missing \
  --cov-report="json:$REPORT_PATH" \
  "$@"
