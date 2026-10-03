#!/usr/bin/env bash
# Build app/web/static/build/app.css with the pinned Tailwind standalone CLI (dev only;
# the Docker image builds its own copy). The binary is checked against its SHA-256.
set -euo pipefail

VERSION="v4.3.3"
case "$(uname -m)" in
  x86_64) ASSET="tailwindcss-linux-x64"; SHA="dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a" ;;
  aarch64|arm64) ASSET="tailwindcss-linux-arm64"; SHA="55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/weightpicks"
BIN="$CACHE/$ASSET-$VERSION"
mkdir -p "$CACHE"
if [[ ! -x "$BIN" ]]; then
  curl -fsSL -o "$BIN.tmp" "https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/$ASSET"
  echo "$SHA  $BIN.tmp" | sha256sum -c --quiet
  chmod +x "$BIN.tmp" && mv "$BIN.tmp" "$BIN"
fi
mkdir -p "$ROOT/app/web/static/build"
cd "$ROOT/app/web/styles"
"$BIN" --input input.css --output "$ROOT/app/web/static/build/app.css" --minify "$@"
