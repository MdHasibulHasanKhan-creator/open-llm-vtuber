#!/usr/bin/env bash
#
# Start Open VRM VTuber (backend + frontend) for development.
#
#   ./scripts/dev.sh                 backend + frontend
#   ./scripts/dev.sh --backend-only
#   ./scripts/dev.sh --frontend-only
#   ./scripts/dev.sh --skip-install
#   ./scripts/dev.sh --port 8765
#   ./scripts/dev.sh --no-reload
#
# The first run creates the Python virtual environment, installs both dependency
# sets, and copies the example config files. Ctrl-C stops both processes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
VENV_PY="$BACKEND_DIR/.venv/bin/python"

BACKEND_ONLY=0
FRONTEND_ONLY=0
SKIP_INSTALL=0
RELOAD=1
PORT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-only)  BACKEND_ONLY=1 ;;
    --frontend-only) FRONTEND_ONLY=1 ;;
    --skip-install)  SKIP_INSTALL=1 ;;
    --no-reload)     RELOAD=0 ;;
    --port)          PORT="${2:?--port needs a value}"; shift ;;
    -h|--help)       sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

step()  { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
warn()  { printf '    \033[33m%s\033[0m\n' "$1"; }
have()  { command -v "$1" >/dev/null 2>&1; }

BACKEND_PID=""
cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    step 'Stopping the backend'
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- #
#  Preconditions
# --------------------------------------------------------------------------- #
if [[ "$FRONTEND_ONLY" -eq 0 ]] && ! have python3 && ! have python; then
  echo "python was not found on PATH. Install Python 3.11+ and try again." >&2
  exit 1
fi
if [[ "$BACKEND_ONLY" -eq 0 ]] && ! have npm; then
  echo "npm was not found on PATH. Install Node.js 20.19+ and try again." >&2
  exit 1
fi

PYTHON="$(have python3 && echo python3 || echo python)"

# --------------------------------------------------------------------------- #
#  Backend setup
# --------------------------------------------------------------------------- #
if [[ "$SKIP_INSTALL" -eq 0 && "$FRONTEND_ONLY" -eq 0 ]]; then
  if [[ ! -x "$VENV_PY" ]]; then
    step 'Creating the Python virtual environment (backend/.venv)'
    "$PYTHON" -m venv "$BACKEND_DIR/.venv"
  fi

  step 'Installing backend dependencies'
  "$VENV_PY" -m pip install --upgrade pip --quiet
  "$VENV_PY" -m pip install -r "$BACKEND_DIR/requirements.txt" --quiet
  if [[ -f "$BACKEND_DIR/requirements-dev.txt" ]]; then
    "$VENV_PY" -m pip install -r "$BACKEND_DIR/requirements-dev.txt" --quiet
  fi

  if [[ -f "$BACKEND_DIR/requirements-local.txt" ]]; then
    printf '\n    Optional offline speech recognition (faster-whisper):\n' >&2
    printf '      pip install -r backend/requirements-local.txt\n' >&2
  fi

  if [[ ! -f "$BACKEND_DIR/.env" ]]; then
    cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
    warn 'Created backend/.env from .env.example - edit it to point at your LLM.'
  fi
fi

# --------------------------------------------------------------------------- #
#  Frontend setup
# --------------------------------------------------------------------------- #
if [[ "$SKIP_INSTALL" -eq 0 && "$BACKEND_ONLY" -eq 0 ]]; then
  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    step 'Installing frontend dependencies (npm install)'
    (cd "$FRONTEND_DIR" && npm install)
  fi
fi

# --------------------------------------------------------------------------- #
#  Run
# --------------------------------------------------------------------------- #
BACKEND_ARGS=(main.py)
if [[ "$PORT" -gt 0 ]]; then
  BACKEND_ARGS+=(--port "$PORT")
fi
if [[ "$RELOAD" -eq 1 ]]; then
  BACKEND_ARGS+=(--reload)
fi

if [[ "$FRONTEND_ONLY" -eq 0 ]]; then
  if [[ "$PORT" -gt 0 ]]; then
    DEFAULT_PORT="$PORT"
  else
    DEFAULT_PORT=8000
  fi
  step "Starting the backend on http://127.0.0.1:$DEFAULT_PORT"
  if [[ "$BACKEND_ONLY" -eq 1 ]]; then
    cd "$BACKEND_DIR" && exec "$VENV_PY" "${BACKEND_ARGS[@]}"
  fi
  (cd "$BACKEND_DIR" && "$VENV_PY" "${BACKEND_ARGS[@]}") &
  BACKEND_PID=$!
fi

if [[ "$BACKEND_ONLY" -eq 0 ]]; then
  step 'Starting the frontend on http://localhost:5173'
  (cd "$FRONTEND_DIR" && npm run dev)
fi
