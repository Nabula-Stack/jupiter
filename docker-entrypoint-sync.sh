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
            echo "Warning: decoded SSH key does not look like an OpenSSH private key, using /app/vcenter_rsa if available."
            if [ -f /app/vcenter_rsa ]; then
                export SSH_KEY_PATH=/app/vcenter_rsa
                chmod 600 /app/vcenter_rsa || true
            fi
        fi
    else
        echo "Warning: failed to decode SSH_PRIVATE_KEY_B64, using /app/vcenter_rsa if available."
        cat /tmp/ssh_key_decode.err || true
        if [ -f /app/vcenter_rsa ]; then
            export SSH_KEY_PATH=/app/vcenter_rsa
            chmod 600 /app/vcenter_rsa || true
        fi
    fi
elif [ -f /app/vcenter_rsa ]; then
    export SSH_KEY_PATH=/app/vcenter_rsa
    chmod 600 /app/vcenter_rsa || true
fi

echo "Fixing Proxmox usernames (appending @pam realm where missing)..."
python manage.py fix_proxmox_usernames || true

echo "Starting sync worker..."
exec python manage.py sync_hosts
