#!/usr/bin/env bash
#
# Usage: ./scripts/start_dev.sh [start|stop|restart|status|logs] [backend|frontend]
#
# Manages both dev servers:
#   - backend:  FastAPI JSON API. Also serves the tracker/widget SDK as static JS
#               (app/static/js/tracker.js, widget.js) — those run on tenants' own
#               sites and stay framework-free regardless of the admin console's stack.
#   - frontend: Next.js admin console (frontend/), talks to the backend via
#               NEXT_PUBLIC_API_BASE_URL in frontend/.env.development
#
#   start    Start both servers detached (survive this terminal closing), then tail
#            their logs. Ctrl-C only stops the tail — the servers keep running. A
#            server already running is left alone (not restarted).
#   stop     Stop both detached servers started by `start`.
#   restart  stop, then start.
#   status   Report whether each server is running and its PID.
#   logs     Tail both logs without starting or stopping anything.
#
# Pass `backend` or `frontend` as a second argument to target just one server, e.g.
#   ./scripts/start_dev.sh stop frontend
#   ./scripts/start_dev.sh logs backend
#
# (default action: start, so plain `./scripts/start_dev.sh` keeps working as before;
#  default target: both)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
# 8001 collides with an unrelated project's own dev server on this machine
# (banking_agent) — this default only matters the first time; PORT= still overrides.
PORT="${PORT:-8011}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
LOG_DIR="${ROOT_DIR}/logs"

BACKEND_LOG_FILE="${LOG_DIR}/trailmind-backend-dev.log"
BACKEND_PID_FILE="${LOG_DIR}/trailmind-backend-dev.pid"
FRONTEND_LOG_FILE="${LOG_DIR}/trailmind-frontend-dev.log"
FRONTEND_PID_FILE="${LOG_DIR}/trailmind-frontend-dev.pid"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

# running_pid <pid_file>
# Echoes the PID if the server tracked by pid_file is actually alive, clearing a
# stale PID file (e.g. left behind after a crash or `kill -9`) as a side effect.
running_pid() {
  local pid_file="$1" pid
  if [[ -f "${pid_file}" ]]; then
    pid="$(cat "${pid_file}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "${pid}"
      return 0
    fi
    rm -f "${pid_file}"
  fi
  return 1
}

targets_for() {
  case "${1:-both}" in
    backend|frontend) echo "$1" ;;
    both|"") echo "backend frontend" ;;
    *)
      echo "Unknown target '$1' (expected: backend, frontend, or omit for both)" >&2
      exit 1
      ;;
  esac
}

tail_logs() {
  local targets files
  targets=($(targets_for "${1:-both}"))
  files=()
  for t in "${targets[@]}"; do
    [[ "$t" == "backend" ]] && files+=("${BACKEND_LOG_FILE}")
    [[ "$t" == "frontend" ]] && files+=("${FRONTEND_LOG_FILE}")
  done
  echo "Tailing ${files[*]} (Ctrl-C stops the tail only, not the servers)..."
  exec tail -f "${files[@]}"
}

do_status() {
  local target="${1:-both}" pid
  for t in $(targets_for "${target}"); do
    if [[ "$t" == "backend" ]]; then
      if pid="$(running_pid "${BACKEND_PID_FILE}")"; then
        echo "Backend is running (PID ${pid}) on http://${HOST}:${PORT}"
      else
        echo "Backend is not running."
      fi
    else
      if pid="$(running_pid "${FRONTEND_PID_FILE}")"; then
        echo "Frontend is running (PID ${pid}) on http://localhost:${FRONTEND_PORT}"
      else
        echo "Frontend is not running."
      fi
    fi
  done
}

stop_one() {
  local name="$1" pid_file="$2" pid
  if pid="$(running_pid "${pid_file}")"; then
    echo "Stopping ${name} (PID ${pid})..."
    kill "${pid}" 2>/dev/null || true
    for _ in {1..10}; do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 0.5
    done
    kill -9 "${pid}" 2>/dev/null || true
    rm -f "${pid_file}"
    echo "${name} stopped."
  else
    echo "${name} is not running."
  fi
}

do_stop() {
  local target="${1:-both}"
  for t in $(targets_for "${target}"); do
    if [[ "$t" == "backend" ]]; then
      stop_one "Backend" "${BACKEND_PID_FILE}"
    else
      stop_one "Frontend" "${FRONTEND_PID_FILE}"
    fi
  done
}

start_backend() {
  local pid
  if pid="$(running_pid "${BACKEND_PID_FILE}")"; then
    echo "Backend is already running (PID ${pid}) on http://${HOST}:${PORT}"
    return
  fi

  echo "Starting backend on http://${HOST}:${PORT}"
  echo "API docs: http://${HOST}:${PORT}/docs"
  echo "Health:   http://${HOST}:${PORT}/health"
  echo "Log:      ${BACKEND_LOG_FILE}"

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
    >>"${BACKEND_LOG_FILE}" 2>&1 &
  disown
  echo $! >"${BACKEND_PID_FILE}"

  for _ in {1..15}; do
    if grep -q "Application startup complete" "${BACKEND_LOG_FILE}" 2>/dev/null; then
      echo "Backend started."
      return
    fi
    if ! kill -0 "$(cat "${BACKEND_PID_FILE}")" 2>/dev/null; then
      echo "Backend failed to start. Recent logs:"
      tail -n 40 "${BACKEND_LOG_FILE}"
      rm -f "${BACKEND_PID_FILE}"
      exit 1
    fi
    sleep 1
  done
  echo "Backend did not report startup within 15 seconds — check ${BACKEND_LOG_FILE}."
}

start_frontend() {
  local pid
  if pid="$(running_pid "${FRONTEND_PID_FILE}")"; then
    echo "Frontend is already running (PID ${pid}) on http://localhost:${FRONTEND_PORT}"
    return
  fi

  echo "Starting frontend on http://localhost:${FRONTEND_PORT}"
  echo "Log: ${FRONTEND_LOG_FILE}"

  (
    cd "${ROOT_DIR}/frontend"
    nohup npm run dev -- --port "${FRONTEND_PORT}" >>"${FRONTEND_LOG_FILE}" 2>&1 &
    disown
    echo $! >"${FRONTEND_PID_FILE}"
  )

  for _ in {1..30}; do
    if grep -qE "Ready in|started server|compiled" "${FRONTEND_LOG_FILE}" 2>/dev/null; then
      echo "Frontend started."
      return
    fi
    if ! kill -0 "$(cat "${FRONTEND_PID_FILE}")" 2>/dev/null; then
      echo "Frontend failed to start. Recent logs:"
      tail -n 40 "${FRONTEND_LOG_FILE}"
      rm -f "${FRONTEND_PID_FILE}"
      exit 1
    fi
    sleep 1
  done
  echo "Frontend did not report startup within 30 seconds — check ${FRONTEND_LOG_FILE}."
}

do_start() {
  local target="${1:-both}"
  for t in $(targets_for "${target}"); do
    if [[ "$t" == "backend" ]]; then
      start_backend
    else
      start_frontend
    fi
  done
  echo
  tail_logs "${target}"
}

ACTION="${1:-start}"
TARGET="${2:-both}"

case "${ACTION}" in
  start) do_start "${TARGET}" ;;
  stop) do_stop "${TARGET}" ;;
  restart)
    do_stop "${TARGET}"
    do_start "${TARGET}"
    ;;
  status) do_status "${TARGET}" ;;
  logs) tail_logs "${TARGET}" ;;
  help|-h|--help)
    echo "Usage: $0 [start|stop|restart|status|logs] [backend|frontend]"
    echo
    echo "  start    Start server(s) detached, then tail logs (Ctrl-C stops the tail"
    echo "           only). Already running -> left alone."
    echo "  stop     Stop the detached server(s) started by 'start'."
    echo "  restart  stop, then start."
    echo "  status   Report whether each server is running and its PID."
    echo "  logs     Tail logs without starting or stopping anything."
    echo
    echo "  Omit the second argument to target both backend and frontend."
    ;;
  *)
    echo "Usage: $0 [start|stop|restart|status|logs] [backend|frontend]" >&2
    exit 1
    ;;
esac
