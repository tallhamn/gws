FROM python:3.12-slim

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
