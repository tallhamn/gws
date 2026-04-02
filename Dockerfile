FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY gws/ gws/
COPY policy.yaml .

RUN pip install --no-cache-dir ".[anthropic]" uvicorn

EXPOSE 8000
CMD ["uvicorn", "gws.api:create_app", "--host", "0.0.0.0", "--port", "8000", "--factory"]
