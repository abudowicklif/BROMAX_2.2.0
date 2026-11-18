FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app.py ./
COPY bromax ./bromax

RUN pip install --no-cache-dir . \
    && mkdir -p /data

ENV DATABASE_PATH=/data/bromax.sqlite3 \
    BROMAX_REQUIRE_AUTH=true

EXPOSE 8765

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8765}"]
