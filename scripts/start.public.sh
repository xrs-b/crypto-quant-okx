#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

echo "[public] PROJECT_DIR=$PROJECT_DIR"
exec "$PROJECT_DIR/scripts/start.sh" "$@"
