FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
# app.py imports alerting at module scope: omitting it breaks the whole
# dashboard, not just the alert path. tests/test_alerting.py guards this.
COPY alerting.py .
COPY templates/ templates/

# Report unhealthy if the app stops responding, so a wedge is visible in
# `docker ps` and an external watchdog / restart policy can act on it.
# Uses Python (slim has no curl).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','5050')+'/', timeout=8)" || exit 1

# --workers 2 --threads 4: more concurrency so one slow request can't starve
#   the server. --timeout 90 --graceful-timeout 30: recycle a stuck worker.
# --max-requests: periodically recycle workers to shed any lingering bad state.
CMD exec gunicorn --bind :${PORT:-5050} --workers 2 --threads 4 \
  --timeout 90 --graceful-timeout 30 \
  --max-requests 1000 --max-requests-jitter 100 app:app
