# ---- NL-to-SQL Commercial Analytics Assistant ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/pharma.db

WORKDIR /app

# Dependencies first: this layer is cached, so code changes rebuild fast
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code, UI, business docs, and the read-only database
COPY app ./app
COPY static ./static
COPY docs ./docs
COPY pharma.db ./pharma.db

# Run as a non-root user (least privilege); the database stays read-only
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]