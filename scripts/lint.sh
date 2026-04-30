#!/usr/bin/env bash
# Run all quality checks against this repo: ruff, shellcheck, dogfood, tests.
# After `pip install -e ".[dev]"` (or `uv sync`), this is the single entry point.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

fail=0

echo "=> ruff check"
if ! ruff check .; then fail=1; fi

echo "=> ruff format --check"
if ! ruff format --check .; then fail=1; fi

echo "=> shellcheck"
if ! shellcheck hooks/hook.sh scripts/*.sh; then fail=1; fi

echo "=> pytest"
if ! pytest -q; then fail=1; fi

echo "=> bully dogfood"
if ! bash scripts/dogfood.sh; then fail=1; fi

if [[ $fail -ne 0 ]]; then
  echo
  echo "One or more checks failed."
  exit 1
fi

echo
echo "All checks passed."
