#!/usr/bin/env bash
# Upgrade a running WeightPicks to another release (BUILD_PLAN §4.5):
#
#   sudo ./scripts/upgrade.sh 1.1.0          # a published release
#   sudo ./scripts/upgrade.sh --build        # rebuild from this clone (for --build installs)
#   (--no-checkout keeps the clone's files as they are)
#
# Steps: health and ledger checks -> verified backup -> check out the release's files ->
# pull and restart (the web container migrates before serving) -> health -> done, or
# the exact rollback commands. Guide: docs/self-host/upgrade.md
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET="" BUILD=0 CHECKOUT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1; shift ;;
    --no-checkout) CHECKOUT=0; shift ;;  # leave the clone's files alone (CI, custom setups)
    -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) TARGET="${1#v}"; shift ;;
  esac
done
docker info >/dev/null 2>&1 || die "can't reach Docker as $(id -un): run it with sudo"
[ -f "$ENV_FILE" ] || die "no .env here; install first (scripts/install.sh)"
[ "$BUILD" = 1 ] || [ -n "$TARGET" ] || die "usage: sudo ./scripts/upgrade.sh VERSION   (or --build)"

CURRENT="$(env_get WP_VERSION)"
CURRENT_REF="$(repo_git rev-parse --short HEAD 2>/dev/null || echo none)"
[ "$BUILD" = 1 ] && TARGET="local"
if [ "$TARGET" = "$CURRENT" ] && [ "$BUILD" = 0 ]; then
  say "Already on $CURRENT"; exit 0
fi

say "Checking the running instance ($CURRENT)"
wait_healthy || die "fix the current instance first; nothing was changed"
compose exec -T worker wp ledger verify || die "ledger verify failed; nothing was changed"

say "Backing up"
BACKUP="$(compose exec -T worker wp backup create --label "pre-$TARGET" | awk '/^created / { print $2 }')"
[ -n "$BACKUP" ] || die "the backup failed; nothing was changed"
compose exec -T worker wp backup verify "$BACKUP" || die "backup $BACKUP failed verification; nothing was changed"

rollback_hint() {
  cat >&2 <<EOF

Upgrade to $TARGET did not finish. To go back to $CURRENT:
  cd $REPO_DIR
  sudo git checkout $CURRENT_REF            # the files you had before
  sudo sed -i 's/^WP_VERSION=.*/WP_VERSION=$CURRENT/' .env
  sudo docker compose up -d
If the new version already migrated the database, also restore the backup taken just now:
  sudo docker compose exec worker wp maintenance restore $BACKUP && sudo docker compose restart
EOF
}
trap 'rollback_hint' ERR

if [ "$BUILD" = 0 ] && [ "$CHECKOUT" = 1 ] && repo_git rev-parse --git-dir >/dev/null 2>&1; then
  if [ -n "$(repo_git status --porcelain --untracked-files=no)" ]; then
    warn "local changes in the clone; leaving its files as they are"
  else
    say "Checking out v$TARGET"
    repo_git fetch --quiet --tags origin
    repo_git checkout --quiet "v$TARGET"
  fi
fi

if [ "$BUILD" = 1 ]; then
  say "Building the image from this clone"
  docker build -q -f "$REPO_DIR/docker/Dockerfile" -t weightpicks:local "$REPO_DIR" >/dev/null
  env_set WP_IMAGE "weightpicks"
  env_set WP_VERSION "local"
else
  case "$(env_get WP_IMAGE)" in "" | weightpicks | *OWNER*) env_set WP_IMAGE "$DEFAULT_IMAGE" ;; esac
  env_set WP_VERSION "$TARGET"
  say "Pulling $(env_get WP_IMAGE):$TARGET"
  pull_images
fi

say "Restarting (the web container migrates the database before serving)"
compose up -d web worker
wait_healthy
compose exec -T worker wp ledger verify
trap - ERR

say "Upgraded $CURRENT -> $TARGET. Backup kept: $BACKUP"
