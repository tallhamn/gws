FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY alembic.ini .
COPY alembic/ alembic/
COPY gws/ gws/
COPY workers.yaml .
COPY policy.yaml .
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN pip install --no-cache-dir ".[anthropic]" uvicorn \
    && chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "gws.api:create_app", "--host", "0.0.0.0", "--port", "8000", "--factory"]
