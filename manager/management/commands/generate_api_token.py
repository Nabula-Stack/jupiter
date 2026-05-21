"""
Management command to generate or retrieve API tokens for users.

Usage:
    python manage.py generate_api_token admin                 # Get or create token for admin user
    python manage.py generate_api_token --list                # List all user tokens
    python manage.py generate_api_token --delete admin        # Delete token for admin user
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = "Generate, list, or delete API tokens for users"

    def add_arguments(self, parser):
        parser.add_argument(
            "username",
            nargs="?",
            type=str,
            help="Username to generate token for",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="List all user tokens",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete token for specified user",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self.list_tokens()
        elif options["delete"]:
            if not options["username"]:
                self.stdout.write(
                    self.style.ERROR("Username required for --delete")
                )
                return
            self.delete_token(options["username"])
        else:
            if not options["username"]:
                self.stdout.write(
                    self.style.ERROR(
                        "Username required. Use --list to see all tokens."
                    )
                )
                return
            self.generate_token(options["username"])

    def generate_token(self, username: str):
        """Generate or retrieve token for a user."""
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f"User '{username}' does not exist")
            )
            return

        if not user.is_staff:
            self.stdout.write(
                self.style.WARNING(
                    f"Warning: User '{username}' is not staff. "
                    "API may require staff privilege."
                )
            )

        token, created = Token.objects.get_or_create(user=user)
        action = "Created" if created else "Retrieved"
        self.stdout.write(
            self.style.SUCCESS(f"{action} token for user '{username}':")
        )
        self.stdout.write(f"\n  Token: {token.key}\n")
        self.stdout.write("Usage in API calls:")
        self.stdout.write('  curl -H "Authorization: Bearer {token}" \\')
        self.stdout.write("       https://jupiter.prod.home/api/v1/hosts/metrics\n")

    def list_tokens(self):
        """List all tokens."""
        tokens = Token.objects.select_related("user").all()
        if not tokens.exists():
            self.stdout.write(self.style.WARNING("No tokens found"))
            return

        self.stdout.write(self.style.SUCCESS("API Tokens:"))
        self.stdout.write("-" * 60)
        for token in tokens:
            self.stdout.write(
                f"  User: {token.user.username:20} | Staff: {str(token.user.is_staff):5} | "
                f"Token: {token.key[:16]}..."
            )
        self.stdout.write("-" * 60)

    def delete_token(self, username: str):
        """Delete token for a user."""
        try:
            user = User.objects.get(username=username)
            token = Token.objects.get(user=user)
            token.delete()
            self.stdout.write(
                self.style.SUCCESS(f"Deleted token for user '{username}'")
            )
        except User.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f"User '{username}' does not exist")
            )
        except Token.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f"No token found for user '{username}'")
            )
