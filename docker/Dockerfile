# syntax=docker/dockerfile:1
# One image, several roles (web, worker, cli) chosen by docker/entrypoint.sh.
# Base images are pinned by digest; Dependabot proposes updates.

FROM ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc AS uv

# ---- build: resolve the locked environment into /opt/venv ----
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
# The project is installed editable, so /app is on sys.path and dev can mount source over it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: venv + source only, non-root, read-only friendly ----
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
RUN groupadd --system --gid 10001 wp \
 && useradd --system --uid 10001 --gid wp --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin wp \
 && mkdir -p /data \
 && chown 10001:10001 /data
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["entrypoint.sh"]
CMD ["web"]
