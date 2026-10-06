# shellcheck shell=bash
# Shared helpers for install.sh and upgrade.sh. Sourced, not run.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-240}"
export DEFAULT_IMAGE="ghcr.io/lotus-infosec/weightpicks"  # used by install.sh and upgrade.sh

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose -f "$REPO_DIR/docker-compose.yml" "$@"; }

# git as root on a clone owned by someone else needs safe.directory.
repo_git() { git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" "$@"; }

env_get() {
  # Value of KEY in .env (without an inline comment), or empty.
  [ -f "$ENV_FILE" ] || return 0
  awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); sub(/[ \t]+#.*$/, ""); print; exit }' "$ENV_FILE"
}

env_set() {
  # Set KEY=VALUE in .env, replacing the line if present. Values are never echoed.
  local key="$1" value="$2" tmp
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    $0 ~ "^" k "=" { if (!done) { print k "=" v; done = 1 }; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

wait_healthy() {
  # Wait until both web and worker report healthy; show logs if they don't.
  local waited=0 status
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    status="$(compose ps --format '{{.Service}}={{.Health}}' 2>/dev/null | sort | tr '\n' ' ')"
    case "$status" in
      *"web=healthy"*"worker=healthy"*) return 0 ;;
    esac
    sleep 3
    waited=$((waited + 3))
  done
  warn "not healthy after ${HEALTH_TIMEOUT}s: ${status:-no containers}"
  compose logs --tail 40 web worker >&2 || true
  return 1
}

latest_release_tag() {
  # The checked-out release tag if HEAD is exactly on one, else the newest v* tag.
  repo_git describe --tags --exact-match --match 'v[0-9]*' HEAD 2>/dev/null ||
    repo_git tag --list 'v[0-9]*' --sort=-v:refname | head -n 1
}
