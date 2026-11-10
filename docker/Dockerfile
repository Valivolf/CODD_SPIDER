FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow

WORKDIR /app

# iputils-ping backs the points directory's "Пинг" check (routes_points.py /
# network_check.py) — the slim base image doesn't ship it. Runs as root in
# this container, which Docker grants CAP_NET_RAW by default, so no extra
# capabilities/setuid dance is needed for unprivileged ICMP.
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY bot ./bot
COPY templates ./templates
COPY static ./static

RUN mkdir -p /app/data /app/backups

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
