FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy

COPY pyproject.toml ./
COPY src ./src

RUN uv sync --no-dev

CMD ["uv", "run", "python", "-m", "wifi_shepard"]
