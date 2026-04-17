#!/usr/bin/env bash
# Run the bully pipeline against every source file in this repo.
# Dogfooding: the tool should lint itself cleanly.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

CONFIG="$REPO_DIR/.bully.yml"
if [[ ! -f "$CONFIG" ]]; then
  echo "no .bully.yml at repo root -- skipping dogfood"
  exit 0
fi

# The repo's own .bully.yml is trusted by definition: the dogfood script runs
# what the repo committed. Skip the machine-local trust gate.
export BULLY_TRUST_ALL=1

# Resolve the bully command. Prefer the installed console script (eats our own
# dog food about the new entrypoint); fall back to the in-tree pipeline.py for
# fresh checkouts where `pip install -e .` has not been run yet.
if command -v bully >/dev/null 2>&1; then
  BULLY=(bully lint)
else
  BULLY=(python3 "$REPO_DIR/pipeline/pipeline.py" --file)
fi

# Sanity preamble: confirm the config parses cleanly before iterating. Bails
# out fast on a malformed `.bully.yml` instead of failing on every file with
# the same parse error.
if command -v bully >/dev/null 2>&1; then
  bully validate --config "$CONFIG" >/dev/null
else
  python3 "$REPO_DIR/pipeline/pipeline.py" --validate --config "$CONFIG" >/dev/null
fi

fail=0
err_file=$(mktemp)
trap 'rm -f "$err_file"' EXIT

# macOS default bash (3.2) lacks mapfile, so stream from find into a loop.
while IFS= read -r file; do
  if ! "${BULLY[@]}" "$file" --config "$CONFIG" >/dev/null 2>"$err_file"; then
    echo "-- $file"
    cat "$err_file"
    fail=1
  fi
done < <(
  find \
    pipeline \
    skills \
    scripts \
    docs \
    examples \
    -type f \
    \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.yml" -o -name "*.yaml" \) \
    ! -path "*/.pytest_cache/*" \
    ! -path "*/__pycache__/*" \
    ! -path "*/.bully/*" \
    2>/dev/null
)

exit $fail
