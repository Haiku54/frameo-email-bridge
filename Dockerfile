FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

RUN mkdir -p inbox processed archive/failed logs data

VOLUME ["/app/config.yaml", "/app/data", "/app/logs", "/app/archive"]

STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
