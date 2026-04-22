FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential curl \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY configs ./configs
COPY scripts ./scripts

RUN pip install --no-cache-dir -e ".[dev]"

EXPOSE 8000
CMD ["python", "-m", "app.main"]
