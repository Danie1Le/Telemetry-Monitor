# Build from the repo root: docker build -f processor/Dockerfile .
FROM python:3.15-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY processor/pyproject.toml processor/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY processor/src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.15-slim-bookworm
RUN useradd --system --uid 10001 app
COPY --from=build /app/.venv /app/.venv
COPY contracts /etc/telemetry/contracts
# sensors.yaml is not baked in: mount it (ConfigMap) at this path.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    CONTRACTS_DIR=/etc/telemetry/contracts SENSORS_CONFIG=/etc/telemetry/sensors.yaml
USER 10001
ENTRYPOINT ["python", "-m", "processor"]
