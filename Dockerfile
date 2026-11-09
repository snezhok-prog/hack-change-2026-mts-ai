FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    STATE_DIR=/app/state
WORKDIR /app
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /app/state /app/data \
    && chown app:app /app/state
COPY consultant.py model_client.py retrieval.py storage.py web.py ./
COPY static/ ./static/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).close()"
CMD ["python", "-m", "uvicorn", "web:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
