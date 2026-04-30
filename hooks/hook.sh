#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

case "${1:-post-tool-use}" in
  session-start)
    exec python3 -m bully session-start
    ;;
  stop)
    exec python3 -m bully stop
    ;;
  subagent-stop)
    exec python3 -m bully subagent-stop
    ;;
  post-tool-use|*)
    exec python3 -m bully --hook-mode
    ;;
esac
