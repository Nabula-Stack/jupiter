"""
Django management command: python manage.py ensure_postgres_db

Ensures the configured PostgreSQL database exists before migrations run.
This helps when a Docker volume already exists but the expected DB was not
initialized from POSTGRES_DB.
"""

import os
import time

from django.core.management.base import BaseCommand, CommandError
import psycopg
from psycopg import sql


class Command(BaseCommand):
    help = "Ensure the configured PostgreSQL database exists"

    def handle(self, *args, **options):
        db_name = os.getenv("DB_NAME", "").strip()
        db_user = os.getenv("DB_USER", "").strip()
        db_password = os.getenv("DB_PASSWORD", "").strip()
        db_host = os.getenv("DB_HOST", "").strip()
        db_port = os.getenv("DB_PORT", "").strip()
        admin_db = os.getenv("DB_ADMIN_DB", "postgres").strip() or "postgres"

        missing = [
            name
            for name, value in {
                "DB_NAME": db_name,
                "DB_USER": db_user,
                "DB_PASSWORD": db_password,
                "DB_HOST": db_host,
                "DB_PORT": db_port,
            }.items()
            if not value
        ]
        if missing:
            raise CommandError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        max_retries = int(os.getenv("DB_BOOTSTRAP_MAX_RETRIES", "30"))
        retry_delay_seconds = float(os.getenv("DB_BOOTSTRAP_RETRY_DELAY", "2"))

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                with psycopg.connect(
                    host=db_host,
                    port=int(db_port),
                    dbname=admin_db,
                    user=db_user,
                    password=db_password,
                    connect_timeout=5,
                    autocommit=True,
                ) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT 1 FROM pg_database WHERE datname = %s",
                            (db_name,),
                        )
                        exists = cur.fetchone() is not None

                        if exists:
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"Database '{db_name}' already exists."
                                )
                            )
                            return

                        cur.execute(
                            sql.SQL("CREATE DATABASE {}").format(
                                sql.Identifier(db_name)
                            )
                        )
                        self.stdout.write(
                            self.style.SUCCESS(f"Created database '{db_name}'.")
                        )
                        return
            except psycopg.Error as exc:
                last_error = exc
                if attempt < max_retries:
                    self.stdout.write(
                        self.style.WARNING(
                            f"PostgreSQL not ready or DB bootstrap failed "
                            f"(attempt {attempt}/{max_retries}); retrying in "
                            f"{retry_delay_seconds}s..."
                        )
                    )
                    time.sleep(retry_delay_seconds)
                    continue

                break

        raise CommandError(
            "Failed to ensure PostgreSQL database exists after "
            f"{max_retries} attempts: {last_error}"
        )
