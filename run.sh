#!/bin/bash
# Launch the Raiden dataset viewer.
#
#   ./run.sh                 # serve on 0.0.0.0:8080
#   RAIDEN_PORT=9000 ./run.sh
#
# Then open http://<this-host-ip>:8080/
set -e
cd "$(dirname "$0")"

HOST="${RAIDEN_HOST:-0.0.0.0}"
PORT="${RAIDEN_PORT:-8080}"

exec uv run uvicorn raiden_viz.app:app --host "$HOST" --port "$PORT" "$@"
