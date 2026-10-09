# syntax=docker/dockerfile:1
# One image, several roles (web, worker, cli) chosen by docker/entrypoint.sh.
# Base images are pinned by digest; Dependabot proposes updates.

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

# ---- css: compile the stylesheet with the pinned Tailwind standalone CLI ----
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS css
ARG TAILWIND_VERSION=v4.3.3
ARG TAILWIND_SHA256_AMD64=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
ARG TAILWIND_SHA256_ARM64=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN case "${TARGETARCH:-amd64}" in \
      amd64) asset=tailwindcss-linux-x64; sha="$TAILWIND_SHA256_AMD64" ;; \
      arm64) asset=tailwindcss-linux-arm64; sha="$TAILWIND_SHA256_ARM64" ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}" \
 && echo "${sha}  /usr/local/bin/tailwindcss" | sha256sum -c - \
 && chmod +x /usr/local/bin/tailwindcss
WORKDIR /src/app/web
COPY app/web/styles ./styles
COPY app/web/templates ./templates
COPY app/web/static/app.js ./static/app.js
RUN cd styles && tailwindcss --input input.css --output /out/app.css --minify

# ---- build: resolve the locked environment into /opt/venv ----
FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
# The project is installed editable, so /app is on sys.path and dev can mount source over it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
# GarminDB gets its own venv: the worker runs it only as a subprocess, so its dependencies
# never mix with the app's. Exact versions and hashes are pinned.
COPY docker/garmindb-requirements.txt /tmp/garmindb-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/garmindb \
 && uv pip install --python /opt/garmindb/bin/python --require-hashes --no-deps \
      -r /tmp/garmindb-requirements.txt

# ---- runtime: venv + source only, non-root, read-only friendly ----
FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="WeightPicks" \
      org.opencontainers.image.description="Self-hosted, fake-money pick'em on one person's weigh-ins" \
      org.opencontainers.image.source="https://github.com/lotus-infosec/weightpicks" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
# Debian security fixes newer than the pinned base image (Trivy): the runtime
# image ships with every published fix even before Dependabot bumps the digest.
# The base image's own pip is never used (the app runs from the uv-built venv): remove it.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y --no-input pip
RUN groupadd --system --gid 10001 wp \
 && useradd --system --uid 10001 --gid wp --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin wp \
 && mkdir -p /data /garmin/.GarminDb \
 && chown 10001:10001 /data /garmin /garmin/.GarminDb \
 && chmod 700 /garmin/.GarminDb
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/garmindb /opt/garmindb
COPY --from=build /app /app
# Outside /app/app so the dev source mount does not hide it (WP_STATIC_BUILD_DIR).
COPY --from=css /out/app.css /app/static-build/app.css
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["entrypoint.sh"]
CMD ["web"]
