#!/usr/bin/env bash
# Install WeightPicks on Ubuntu 22.04 or 24.04 (amd64 or arm64), from a clone of the repo:
#
#   sudo git clone https://github.com/lotus-infosec/weightpicks.git /opt/weightpicks
#   cd /opt/weightpicks && sudo git checkout v1.0.0     # the release you want
#   sudo ./scripts/install.sh
#
# Safe to run again: it never replaces an existing .env or its APP_SECRET_KEY.
# Guide: docs/self-host/install.md
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/install.sh [options]

  --hostname NAME          public hostname behind the Cloudflare Tunnel (picks.example.com)
  --version X.Y.Z          image version to run (default: the checked-out release tag)
  --build                  build the image from this clone instead of pulling it
  --bind ADDR:PORT         where the web port listens on the host (default 127.0.0.1:8000)
  --timezone ZONE          IANA time zone pre-filled in /setup (default: the host's)
  --unit lb|kg             weight unit pre-filled in /setup (default: lb)
  --tunnel-token-file F    read the Cloudflare Tunnel token from a file (else a hidden prompt)
  --no-tunnel              don't install cloudflared (do it later, see docs/self-host/cloudflared.md)
  --prereqs-only           only install Docker and cloudflared packages, then stop
  --check                  only run the checks; change nothing
  --yes                    no questions (use the defaults and the flags above)
  -h, --help               this help
EOF
}

HOSTNAME_ARG="" VERSION_ARG="" BUILD=0 BIND="127.0.0.1:8000" TZ_ARG="" UNIT="lb"
TOKEN_FILE="" TUNNEL=1 PREREQS_ONLY=0 CHECK_ONLY=0 ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) HOSTNAME_ARG="${2:?}"; shift 2 ;;
    --version) VERSION_ARG="${2:?}"; shift 2 ;;
    --build) BUILD=1; shift ;;
    --bind) BIND="${2:?}"; shift 2 ;;
    --timezone) TZ_ARG="${2:?}"; shift 2 ;;
    --unit) UNIT="${2:?}"; shift 2 ;;
    --tunnel-token-file) TOKEN_FILE="${2:?}"; shift 2 ;;
    --no-tunnel) TUNNEL=0; shift ;;
    --prereqs-only) PREREQS_ONLY=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

ask() {
  # ask "Question" default -> answer on stdout (default with --yes or no terminal)
  local answer=""
  if [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; then
    read -r -p "$1 [${2}]: " answer
  fi
  printf '%s' "${answer:-$2}"
}

# ---- 1. checks ---------------------------------------------------------------------------

preflight() {
  say "Checking this machine"
  [ -r /etc/os-release ] || die "can't read /etc/os-release; this installer is for Ubuntu"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04 | ubuntu:24.04) echo "  OS: Ubuntu $VERSION_ID" ;;
    *) warn "tested on Ubuntu 22.04 and 24.04; this is ${PRETTY_NAME:-unknown}" ;;
  esac
  case "$(uname -m)" in
    x86_64 | aarch64) echo "  CPU: $(uname -m)" ;;
    *) die "unsupported CPU $(uname -m); images exist for amd64 and arm64" ;;
  esac
  local mem_mb disk_gb
  mem_mb="$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo)"
  echo "  RAM: ${mem_mb} MB"
  [ "$mem_mb" -ge 1800 ] || warn "less than 2 GB of RAM; 4 GB is recommended"
  disk_gb="$(df -Pk /var/lib 2>/dev/null | awk 'NR == 2 { print int($4 / 1048576) }')"
  echo "  Free disk under /var/lib: ${disk_gb:-?} GB"
  [ "${disk_gb:-0}" -ge 10 ] || warn "less than 10 GB free; 20 GB is recommended"
  if command -v timedatectl >/dev/null 2>&1; then
    if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
      echo "  Clock: synchronized"
    else
      warn "the clock isn't NTP-synchronized; bet locks depend on it (sudo timedatectl set-ntp true)"
    fi
  fi
  if command -v docker >/dev/null 2>&1; then
    local engine
    engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
    echo "  Docker: ${engine:-installed, daemon not reachable}"
    if [ -n "$engine" ] && [ "${engine%%.*}" -lt 25 ]; then
      die "Docker Engine $engine is too old; 25 or newer is needed"
    fi
    docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (apt install docker-compose-plugin)"
  else
    echo "  Docker: not installed (will install)"
  fi
  local port="${BIND##*:}"
  if command -v ss >/dev/null 2>&1 && ss -Hltn "sport = :$port" | grep -q .; then
    if ! compose ps --status running 2>/dev/null | grep -q web; then
      die "port $port is already in use; pick another with --bind 127.0.0.1:PORT"
    fi
  fi
}

need_root() {
  # Installing packages and system services needs root; running the app only needs Docker.
  [ "$(id -u)" = 0 ] || die "$1 needs root: run it with sudo (sudo ./scripts/install.sh ...)"
}

# ---- 2. packages -------------------------------------------------------------------------

apt_install() { DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null; }

install_docker() {
  command -v docker >/dev/null 2>&1 && return 0
  need_root "installing Docker"
  say "Installing Docker Engine from Docker's apt repository"
  apt-get update -qq >/dev/null
  apt_install ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -qq >/dev/null
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  if [ -d /run/systemd/system ]; then
    systemctl enable --now docker >/dev/null
  fi
}

install_cloudflared() {
  command -v cloudflared >/dev/null 2>&1 && return 0
  need_root "installing cloudflared"
  say "Installing cloudflared from Cloudflare's apt repository"
  apt-get update -qq >/dev/null
  apt_install ca-certificates curl
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-public-v2.gpg -o /usr/share/keyrings/cloudflare-public-v2.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-public-v2.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    >/etc/apt/sources.list.d/cloudflared.list
  apt-get update -qq >/dev/null
  apt_install cloudflared
}

# ---- 3. settings -------------------------------------------------------------------------

host_timezone() {
  timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC
}

write_env() {
  if [ -f "$ENV_FILE" ] && grep -q '^APP_SECRET_KEY=.' "$ENV_FILE"; then
    say "Keeping the existing .env (and its APP_SECRET_KEY)"
  else
    say "Writing .env (mode 600)"
    local hostname="$HOSTNAME_ARG"
    if [ -z "$hostname" ] && [ "$TUNNEL" = 1 ]; then
      hostname="$(ask "Public hostname for the tunnel (e.g. picks.example.com)" "")"
      [ -n "$hostname" ] || die "a public hostname is needed (or use --no-tunnel to set it up later)"
    fi
    umask 077
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    env_set APP_SECRET_KEY "$(head -c 48 /dev/urandom | base64 -w0)"
    if [ -n "$hostname" ]; then
      env_set WP_BASE_URL "https://${hostname#https://}"
    else
      env_set WP_BASE_URL "http://$BIND"
    fi
    env_set WP_TIMEZONE "${TZ_ARG:-$(host_timezone)}"
    env_set WP_UNIT "$UNIT"
  fi
  env_set WP_BIND "$BIND"
  if [ "$BUILD" = 1 ]; then
    env_set WP_IMAGE "weightpicks"
    env_set WP_VERSION "local"
  else
    local version="${VERSION_ARG:-}"
    if [ -z "$version" ]; then
      version="$(env_get WP_VERSION)"
      case "$version" in "" | local) version="$(latest_release_tag)" ;; esac
    fi
    [ -n "$version" ] || die "no release found; pass --version X.Y.Z or --build"
    case "$(env_get WP_IMAGE)" in "" | weightpicks | *OWNER*) env_set WP_IMAGE "$DEFAULT_IMAGE" ;; esac
    env_set WP_VERSION "${version#v}"
  fi
}

# ---- 4. run ------------------------------------------------------------------------------

start_app() {
  if [ "$BUILD" = 1 ]; then
    say "Building the image from this clone (a few minutes)"
    docker build -q -f "$REPO_DIR/docker/Dockerfile" -t weightpicks:local "$REPO_DIR" >/dev/null
  else
    say "Pulling $(env_get WP_IMAGE):$(env_get WP_VERSION)"
    pull_images
  fi
  say "Starting WeightPicks"
  compose up -d web worker
  wait_healthy || die "the app didn't become healthy; see the logs above and docs/self-host/troubleshooting.md"
}

show_setup_token() {
  local token
  token="$(compose logs web 2>/dev/null | grep -o 'SETUP TOKEN: [A-Za-z0-9_-]*' | tail -n 1 || true)"
  echo
  if [ -n "$token" ]; then
    say "Finish in the browser: $(env_get WP_BASE_URL)/setup"
    echo "  $token   (valid 24 hours; print it again: sudo docker compose logs web | grep 'SETUP TOKEN')"
  else
    say "Setup is already done. Open $(env_get WP_BASE_URL)"
  fi
}

setup_tunnel() {
  [ "$TUNNEL" = 1 ] || return 0
  if systemctl is-active --quiet cloudflared 2>/dev/null; then
    say "cloudflared is already running as a service"
    return 0
  fi
  install_cloudflared
  local token=""
  if [ -n "$TOKEN_FILE" ]; then
    token="$(tr -d '[:space:]' <"$TOKEN_FILE")"
  elif [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; then
    echo "Paste the tunnel token from Cloudflare (Zero Trust > Networks > Tunnels > your tunnel)."
    echo "Input is hidden. Leave empty to skip and do it later (docs/self-host/cloudflared.md)."
    read -r -s -p "Tunnel token: " token
    echo
  fi
  if [ -z "$token" ]; then
    warn "no tunnel token; later run: sudo cloudflared service install <TOKEN>"
    return 0
  fi
  need_root "installing the tunnel service"
  say "Installing the tunnel as a system service"
  cloudflared service install "$token" >/dev/null 2>&1 || die "cloudflared service install failed (check the token)"
  unset token
  sleep 3
  systemctl is-active --quiet cloudflared || die "the cloudflared service isn't running: journalctl -u cloudflared"
  echo "  In the Cloudflare dashboard, the tunnel's public hostname must point to http://$BIND"
}

firewall_advice() {
  cat <<EOF

Recommended hardening (not changed by this script):
  sudo ufw default deny incoming && sudo ufw allow from <your LAN>/24 to any port 22 && sudo ufw enable
  The app listens on $BIND only; the tunnel is the only way in from the internet.
Next: docs/self-host/install.md (first run), garmin.md (Garmin login), operations.md (backups).
EOF
}

main() {
  preflight
  [ "$CHECK_ONLY" = 1 ] && { say "Checks done; nothing changed"; exit 0; }
  install_docker
  if [ "$PREREQS_ONLY" = 1 ]; then
    if [ "$TUNNEL" = 1 ]; then install_cloudflared; fi
    say "Packages installed"; exit 0
  fi
  docker info >/dev/null 2>&1 || die "can't reach Docker as $(id -un): run it with sudo"
  write_env
  start_app
  setup_tunnel
  show_setup_token
  firewall_advice
}

main "$@"
