FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock RUN.md ./
RUN uv sync --frozen --no-dev

COPY . .

ENTRYPOINT ["uv", "run", "--no-dev", "--frozen"]
