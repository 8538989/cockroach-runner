FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY companion/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY companion/service.py ./service.py
COPY companion/static ./static

RUN useradd --system --uid 10001 --create-home runner \
    && mkdir -p /data \
    && chown runner:runner /data
USER runner

EXPOSE 8790
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=3)"]

CMD ["python", "/app/service.py", "--host", "0.0.0.0", "--port", "8790", "--data", "/data", "--static", "/app/static"]
