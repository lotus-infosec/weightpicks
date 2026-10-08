#!/bin/sh
# Roles: web | worker | cli <args> | garmin-login
set -eu

role="${1:-web}"
[ "$#" -gt 0 ] && shift

case "$role" in
  web)
    # Apply a staged restore/reset first (waits for the worker to stop), then migrate
    # (file-locked) before serving; extra args (e.g. --reload in dev) go to uvicorn.
    # The app logs each request itself (route templates only), so the access log is off.
    wp maintenance apply
    wp migrate
    exec uvicorn app.web.main:create_app --factory \
      --host 0.0.0.0 --port 8000 --no-server-header --no-access-log "$@"
    ;;
  worker)
    # Never open the database while a restore/reset is staged.
    wp maintenance wait
    if [ "${1:-}" = "--reload" ]; then
      exec watchfiles --filter python "python -m app.worker.main" /app/app
    fi
    exec python -m app.worker.main
    ;;
  cli)
    exec wp "$@"
    ;;
  garmin-login)
    # GarminDB's own venv; -P keeps the script's folder off sys.path.
    exec /opt/garmindb/bin/python -P /app/app/providers/garmin_login.py
    ;;
  *)
    echo "unknown role: $role (expected web, worker, cli or garmin-login)" >&2
    exit 64
    ;;
esac
