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

EXPOSE 8000

CMD ["sh", "-c", "python manage.py ensure_postgres_db && python manage.py migrate --noinput && python manage.py collectstatic --noinput --clear && (python manage.py fix_proxmox_usernames || true) && python manage.py ensure_default_superuser && exec uvicorn core.asgi:application --host 0.0.0.0 --port \"${WEB_INTERNAL_PORT:-8000}\" --ws websockets --timeout-keep-alive 600"]
