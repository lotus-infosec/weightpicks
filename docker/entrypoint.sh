#!/bin/sh
# Roles: web | worker | cli <args> | garmin-login
set -eu

role="${1:-web}"
[ "$#" -gt 0 ] && shift

case "$role" in
  web)
    # Migrate (file-locked) before serving; extra args (e.g. --reload in dev) go to uvicorn.
    wp migrate
    exec uvicorn app.web.main:create_app --factory \
      --host 0.0.0.0 --port 8000 --no-server-header "$@"
    ;;
  worker)
    if [ "${1:-}" = "--reload" ]; then
      exec watchfiles --filter python "python -m app.worker.main" /app/app
    fi
    exec python -m app.worker.main
    ;;
  cli)
    exec wp "$@"
    ;;
  garmin-login)
    echo "garmin-login is not available yet (arrives in STAGE10)." >&2
    exit 1
    ;;
  *)
    echo "unknown role: $role (expected web, worker, cli or garmin-login)" >&2
    exit 64
    ;;
esac
