# Inkplate weather server: FastAPI + Playwright (headless Chromium) -> 1200x825 8-gray PNG.
#
# Base image: python:3.12-slim-bookworm + `playwright install --with-deps --only-shell chromium`.
#   Why not mcr.microsoft.com/playwright/python? That image is ~2 GB because it ships Chromium, Firefox and WebKit
#   plus their deps. We only need Chromium's headless shell, which keeps the image well under half that size.
#   Both are published for linux/amd64 and linux/arm64, so this builds on the Pi or with buildx.
# Playwright is pinned (PLAYWRIGHT_VERSION) so the bundled Chromium is reproducible and matches the dev venv.
#   It must satisfy server/pyproject.toml (`playwright>=1.47`).
# The renderer must launch Chromium the default way (headless, no `channel="chromium"`), because only the headless
#   shell is installed. Set PLAYWRIGHT_INSTALL_ARGS="" to install the full Chromium build too (+~350 MB).
#
# Conservative syntax (no heredocs, no RUN --mount) so it builds on old engines (Docker 20.10, buildx 0.6).
#
#   docker build -t inkplate-weather:latest server
#   docker buildx build --platform linux/arm64 -t inkplate-weather:arm64 --load server

FROM python:3.12-slim-bookworm

ARG PLAYWRIGHT_VERSION=1.64.1
ARG PLAYWRIGHT_INSTALL_ARGS=--only-shell

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=America/Chicago

# tzdata: honour TZ for libc/Chromium local time (Python's zoneinfo also has the tzdata wheel).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Chromium (headless shell only) + its system libraries. Kept in its own layer so code changes don't redo it.
RUN pip install "playwright==${PLAYWRIGHT_VERSION}" \
 && playwright install --with-deps ${PLAYWRIGHT_INSTALL_ARGS} chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache \
 && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

WORKDIR /app

# Runtime dependencies from pyproject (no dev extras), cached separately from the app code.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt "playwright==${PLAYWRIGHT_VERSION}" \
 && rm /tmp/requirements.txt

# App code, templates, bundled assets (fonts/icons) and fixtures. No internet is needed at render time.
COPY . ./
RUN pip install --no-deps . \
 && rm -rf build *.egg-info

# Non-root user. Writable state lives in /cache (volume). The relative defaults also land in the volumes:
#   /app/cache  -> /cache   (config `server.cache_dir: cache`, resolved from WORKDIR /app)
#   /app/config -> /config  (so a cwd-relative `config/config.yaml` / `config/config.example.yaml` resolves too)
# A bind-mounted ./cache must be writable by uid 1000 (see docs/DEPLOY.md).
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /cache /config \
 && chown app:app /cache \
 && rm -rf /app/cache /app/config \
 && ln -s /cache /app/cache \
 && ln -s /config /app/config

ENV INKPLATE_CONFIG=/config/config.yaml \
    INKPLATE_CACHE_DIR=/cache

USER app

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
