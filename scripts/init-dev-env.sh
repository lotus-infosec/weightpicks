#!/usr/bin/env bash
# Create the local dev settings file used by docker-compose.dev.yml.
# The file lives outside the repo so it can never be committed. A random
# APP_SECRET_KEY is generated and written directly; it is never printed.
set -euo pipefail

dir="${XDG_CONFIG_HOME:-$HOME/.config}/weightpicks"
file="$dir/dev.env"

umask 077
mkdir -p "$dir"
touch "$file"
chmod 600 "$file"

if grep -q '^APP_SECRET_KEY=.' "$file"; then
  echo "dev settings already present: $file (left unchanged)"
  exit 0
fi

key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
cat >>"$file" <<EOF
APP_ENV=dev
DATA_PROVIDER=simulated
SIM_SEED=42
LOG_LEVEL=INFO
LOG_FORMAT=console
WP_BIND=127.0.0.1:8000
WP_BASE_URL=http://127.0.0.1:8000
APP_SECRET_KEY=$key
EOF
echo "wrote dev settings: $file (mode 600)"
