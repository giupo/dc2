# DynaCamp: campaign engine + HTTP API + web UI in one image.
#
#   docker build -t dynacamp .
#   docker run --rm -p 8080:8080 -p 50555:50555 dynacamp                       # Syria, waits for DCS
#   docker run --rm -p 8080:8080 dynacamp scenarios/western_georgia.toml --no-dcs --compression 600
#
# UI on http://localhost:8080/, API on /api, docs on /docs. Port 50555 is the
# DCS bridge (the mission's Lua script connects to it: set its host to this
# machine). Scenarios are in /app/scenarios: mount your own with
#   -v $PWD/my_scenarios:/app/scenarios:ro

# ---------------------------------------------------------------- web UI
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------------------------------------------------------------- python deps
FROM python:3.14-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached until uv.lock changes), then the project itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM python:3.14-slim
LABEL org.opencontainers.image.title="DynaCamp" \
      org.opencontainers.image.description="Headless dynamic military campaign engine with a live DCS adapter"
RUN useradd --create-home --uid 1000 dynacamp && mkdir /data && chown dynacamp /data
WORKDIR /app
COPY --from=app /app/.venv /app/.venv
COPY --from=web /web/dist /app/web/dist
COPY scenarios/ /app/scenarios/
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER dynacamp

EXPOSE 8080 50555
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/campaign', timeout=4)"
# Ctrl-C semantics: the engine stops cleanly on SIGINT.
STOPSIGNAL SIGINT

ENTRYPOINT ["dynacamp", "serve", "--host", "0.0.0.0", "--api-host", "0.0.0.0", "--web-dir", "/app/web/dist"]
# The campaign is saved in /data (mount a volume there) and resumed on restart.
VOLUME /data
CMD ["scenarios/syria_m5.toml", "--save", "/data/campaign.dcsave"]
