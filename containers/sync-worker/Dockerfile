FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN python -m ensurepip --upgrade && \
    pip install --no-cache-dir --upgrade pip setuptools==69.5.1 wheel

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY . .

CMD ["sh", "-c", "python manage.py fix_proxmox_usernames || true; exec python manage.py sync_hosts"]
