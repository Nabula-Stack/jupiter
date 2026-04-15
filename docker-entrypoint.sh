#!/bin/bash
set -euo pipefail

echo "Waiting for database..."
python -c "
import time, psycopg, os
while True:
    try:
        psycopg.connect(
            host=os.environ.get('DB_HOST', 'db'),
            port=int(os.environ.get('DB_PORT', '5432')),
            dbname=os.environ.get('DB_NAME', 'mydatabase'),
            user=os.environ.get('DB_USER', 'admin'),
            password=os.environ.get('DB_PASSWORD', 'mypassword'),
        )
        break
    except psycopg.OperationalError:
        time.sleep(1)
"
echo "Database is ready."

# Build SSH key file from secret env var (no host volume mount needed)
export SSH_KEY_PATH="${SSH_KEY_CONTAINER_PATH:-/run/secrets/ssh_key}"
if [ -n "${SSH_PRIVATE_KEY_B64:-}" ]; then
    install --directory --mode=0700 /run/secrets
    sanitized_b64="$(printf '%s' "${SSH_PRIVATE_KEY_B64}" | tr -d '\r\n\t ')"
    if printf '%s' "${sanitized_b64}" | base64 --decode > "${SSH_KEY_PATH}" 2>/tmp/ssh_key_decode.err; then
        if grep --quiet "BEGIN OPENSSH PRIVATE KEY" "${SSH_KEY_PATH}"; then
            chmod 600 "${SSH_KEY_PATH}"
        else
            echo "Warning: decoded SSH key does not look like an OpenSSH private key, using /app/nebula_rsa if available."
            if [ -f /app/nebula_rsa ]; then
                export SSH_KEY_PATH=/app/nebula_rsa
                chmod 600 /app/nebula_rsa || true
            fi
        fi
    else
        echo "Warning: failed to decode SSH_PRIVATE_KEY_B64, using /app/nebula_rsa if available."
        cat /tmp/ssh_key_decode.err || true
        if [ -f /app/nebula_rsa ]; then
            export SSH_KEY_PATH=/app/nebula_rsa
            chmod 600 /app/nebula_rsa || true
        fi
    fi
elif [ -f /app/nebula_rsa ]; then
    export SSH_KEY_PATH=/app/nebula_rsa
    chmod 600 /app/nebula_rsa || true
fi

echo "Running migrations..."
python manage.py migrate --noinput

if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    echo "Ensuring Django superuser exists..."
    python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
username = '${DJANGO_SUPERUSER_USERNAME}'
email = '${DJANGO_SUPERUSER_EMAIL:-admin@example.com}'
password = '${DJANGO_SUPERUSER_PASSWORD}'
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username=username, email=email, password=password)
    print('Created superuser:', username)
else:
    print('Superuser already exists:', username)
"
fi

echo "Fixing Proxmox usernames (appending @pam realm where missing)..."
python manage.py fix_proxmox_usernames || true

echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

echo "Starting Daphne on 0.0.0.0:8000..."
exec daphne -b 0.0.0.0 -p 8000 core.asgi:application
