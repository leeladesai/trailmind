#!/usr/bin/env bash
#
# Usage: ./scripts/start_dev.sh [start|stop|restart|status|logs]
#
# This script manages the backend only — a pure FastAPI JSON API (the admin console
# moved to a separate Next.js app in frontend/, started separately via `npm run dev`
# there). The API also still serves the tracker/widget SDK as static JS
# (app/static/js/tracker.js, widget.js) — those run on tenants' own sites and stay
# framework-free regardless of what the admin console's stack is.
#
#   start    Start the server detached (survives this terminal closing), then tail its
#            log. Ctrl-C only stops the tail — the server keeps running. If it's already
#            running, just attaches to the log instead of starting a second instance.
#   stop     Stop the detached server started by `start`.
#   restart  stop, then start.
#   status   Report whether the server is running and its PID.
#   logs     Tail the log without starting or stopping anything.
#
# (default: start, so plain `./scripts/start_dev.sh` keeps working as before)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
# 8001 collides with an unrelated project's own dev server on this machine
# (banking_agent) — this default only matters the first time; PORT= still overrides.
PORT="${PORT:-8011}"
LOG_DIR="${ROOT_DIR}/logs"
LOG_FILE="${LOG_DIR}/trailmind-dev.log"
PID_FILE="${LOG_DIR}/trailmind-dev.pid"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

running_pid() {
  # Echoes the PID if the server started by this script is actually alive, clearing a
  # stale PID file (e.g. left behind after a crash or `kill -9`) as a side effect.
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "${pid}"
      return 0
    fi
    rm -f "${PID_FILE}"
  fi
  return 1
}

tail_logs() {
  echo "Tailing ${LOG_FILE} (Ctrl-C stops the tail only, not the server)..."
  exec tail -f "${LOG_FILE}"
}

do_status() {
  local pid
  if pid="$(running_pid)"; then
    echo "TrailMind is running (PID ${pid}) on http://${HOST}:${PORT}"
  else
    echo "TrailMind is not running."
  fi
}

do_stop() {
  local pid
  if pid="$(running_pid)"; then
    echo "Stopping TrailMind (PID ${pid})..."
    kill "${pid}" 2>/dev/null || true
    for _ in {1..10}; do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "${pid}" 2>/dev/null || true
    rm -f "${PID_FILE}"
    echo "Stopped."
  else
    echo "TrailMind is not running."
  fi
}

do_start() {
  local pid
  if pid="$(running_pid)"; then
    echo "TrailMind backend is already running (PID ${pid}) on http://${HOST}:${PORT}"
    tail_logs
  fi

  echo "Starting TrailMind backend on http://${HOST}:${PORT}"
  echo "API docs: http://${HOST}:${PORT}/docs"
  echo "Health:   http://${HOST}:${PORT}/health"
  echo "Log:      ${LOG_FILE}"
  echo
  echo "Admin console (separate app): cd frontend && npm run dev  ->  http://localhost:3000"

  # --reload watches the whole repo root by default, which now also contains
  # frontend/node_modules (hundreds of thousands of files) — a single file changing
  # in there (e.g. during `npm install`) was enough to trigger a reload cycle that
  # crashed the reloader outright. Excluding it (and other noisy/irrelevant dirs)
  # keeps the watch scoped to what can actually affect the backend.
  nohup uv run uvicorn app.asgi:app --reload --host "${HOST}" --port "${PORT}" \
    --reload-exclude 'frontend/*' \
    --reload-exclude '.venv/*' \
    --reload-exclude 'venv/*' \
    --reload-exclude 'logs/*' \
    --reload-exclude 'chroma_data/*' \
    --reload-exclude '.git/*' \
    >>"${LOG_FILE}" 2>&1 &
  disown
  echo $! >"${PID_FILE}"

  for _ in {1..15}; do
    if grep -q "Application startup complete" "${LOG_FILE}" 2>/dev/null; then
      echo "TrailMind started."
      tail_logs
    fi
    if ! kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
      echo "TrailMind failed to start. Recent logs:"
      tail -n 40 "${LOG_FILE}"
      rm -f "${PID_FILE}"
      exit 1
    fi
    sleep 1
  done

  echo "TrailMind did not report startup within 15 seconds. Tailing logs..."
  tail_logs
}

case "${1:-start}" in
  start) do_start ;;
  stop) do_stop ;;
  restart)
    do_stop
    do_start
    ;;
  status) do_status ;;
  logs) tail_logs ;;
  help|-h|--help)
    echo "Usage: $0 [start|stop|restart|status|logs]"
    echo
    echo "  start    Start the server detached, then tail its log (Ctrl-C stops the"
    echo "           tail only). Already running -> just attaches to the log."
    echo "  stop     Stop the detached server started by 'start'."
    echo "  restart  stop, then start."
    echo "  status   Report whether the server is running and its PID."
    echo "  logs     Tail the log without starting or stopping anything."
    ;;
  *)
    echo "Usage: $0 [start|stop|restart|status|logs]" >&2
    exit 1
    ;;
esac
