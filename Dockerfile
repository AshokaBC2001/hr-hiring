FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HR_APP_HOST=0.0.0.0 \
    HR_APP_PORT=8000 \
    HR_PUBLIC_HOSTING=1 \
    TALENT_HUB_MODE=mongo \
    TALENT_HUB_API_URL= \
    HR_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py run.py README.md ARCHITECTURE.md ./
COPY hiring ./hiring
COPY employees ./employees
COPY shared ./shared
COPY web ./web
COPY samples ./samples
COPY scripts ./scripts

RUN mkdir -p /app/data/inbox /app/data/outbox /app/data/run-logs /app/data/notes

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; p = os.environ.get('PORT') or os.environ.get('HR_APP_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{p}/api/health', timeout=3)"

CMD ["python", "app.py"]
