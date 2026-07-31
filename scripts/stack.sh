#!/usr/bin/env bash
# Start/stop the three long-running processes without Docker.
#
#   api    FastAPI    serves /docs and the REST API
#   worker Celery     does the scraping, matching and sending
#   beat   Celery Beat ticks every few minutes and queues whatever is due
#
# Beat is the piece that makes this autonomous: nothing needs to be triggered
# by hand once it is up.

set -euo pipefail
cd "$(dirname "$0")/.."

RUN_DIR=.run
LOG_DIR="$RUN_DIR/logs"
VENV=.venv/bin
CELERY_APP=app.scheduler.celery_app.celery_app

mkdir -p "$LOG_DIR"

_pidfile() { echo "$RUN_DIR/$1.pid"; }

_running() {
  local pid
  pid=$(cat "$(_pidfile "$1")" 2>/dev/null || echo "")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

_spawn() {
  local name=$1; shift
  if _running "$name"; then
    echo "  $name already running (pid $(cat "$(_pidfile "$name")"))"
    return
  fi
  # setsid-less nohup: survives this shell, dies on `stack.sh stop`.
  nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  echo $! >"$(_pidfile "$name")"
  echo "  $name started (pid $!) -> $LOG_DIR/$name.log"
}

_kill() {
  local name=$1 pid
  pid=$(cat "$(_pidfile "$name")" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    # TERM lets Celery finish the task in flight; the loop is the impatience.
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$pid" 2>/dev/null || true
    echo "  $name stopped"
  else
    echo "  $name not running"
  fi
  rm -f "$(_pidfile "$name")"
}

_require_deps() {
  [ -x "$VENV/python" ] || { echo "!! .venv missing — run: make install"; exit 1; }

  # Probed through the venv rather than with pg_isready/redis-cli. Those live
  # in /opt/homebrew/bin, which is absent from a non-login shell's PATH, so
  # relying on them reports a healthy database as "not reachable". Connecting
  # the same way the application does is both portable and a truer check —
  # it validates the URLs in .env, not just that something is on the port.
  "$VENV/python" - <<'PY' || exit 1
import sys
from app.core.config import get_settings

settings = get_settings()
problems = []

try:
    import sqlalchemy as sa
    sa.create_engine(settings.database_url, pool_pre_ping=True).connect().close()
except Exception as exc:
    problems.append(f"PostgreSQL unreachable via DATABASE_URL: {type(exc).__name__}")

try:
    import redis
    redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3).ping()
except Exception as exc:
    problems.append(f"Redis unreachable via REDIS_URL: {type(exc).__name__}")

for problem in problems:
    print(f"!! {problem}")
if problems:
    print("   start them with: brew services start postgresql@17 redis")
    sys.exit(1)
PY
}

case "${1:-}" in
  start)
    _require_deps
    echo "applying migrations..."
    "$VENV/alembic" upgrade head >>"$LOG_DIR/migrate.log" 2>&1
    echo "starting:"
    _spawn api    "$VENV/uvicorn" app.main:app --host 127.0.0.1 --port 8000 --log-level warning
    _spawn worker "$VENV/celery" -A "$CELERY_APP" worker --loglevel=info \
                  --concurrency=2 --queues=scrape,notify,maintenance,celery
    _spawn beat   "$VENV/celery" -A "$CELERY_APP" beat --loglevel=info \
                  --schedule="$RUN_DIR/celerybeat-schedule"
    sleep 4
    echo "applying config/tracker.yml:"
    "$VENV/python" -m app.cli sync | sed 's/^/  /'
    echo
    echo "running. api: http://127.0.0.1:8000/docs   logs: $LOG_DIR/"
    ;;
  stop)
    echo "stopping:"
    for name in beat worker api; do _kill "$name"; done
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    for name in api worker beat; do
      if _running "$name"; then echo "  $name    up   (pid $(cat "$(_pidfile "$name")"))"
      else echo "  $name    down"; fi
    done
    ;;
  logs)
    tail -f "$LOG_DIR"/*.log
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
