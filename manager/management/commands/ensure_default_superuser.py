"""
Django management command: python manage.py ensure_default_superuser

Creates or updates a default Django superuser from environment variables.
Required environment variables:
  - DJANGO_SUPERUSER_USERNAME
  - DJANGO_SUPERUSER_PASSWORD
  - DJANGO_SUPERUSER_EMAIL
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update the default Django superuser from environment variables"

    def handle(self, *args, **options):
        username = os.getenv("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD", "").strip()
        email = os.getenv("DJANGO_SUPERUSER_EMAIL", "").strip()

        missing = [
            name
            for name, value in {
                "DJANGO_SUPERUSER_USERNAME": username,
                "DJANGO_SUPERUSER_PASSWORD": password,
                "DJANGO_SUPERUSER_EMAIL": email,
            }.items()
            if not value
        ]
        if missing:
            raise CommandError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )

        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created default superuser '{username}' from environment."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated default superuser '{username}' from environment."
                )
            )
