"""
Token-based authentication for Jupiter API.
Supports both Django session auth (browser) and token auth (programmatic).
"""

from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from ninja.security import HttpBearer


class TokenAuth(HttpBearer):
    """
    Bearer token authentication.
    Usage: Authorization: Bearer YOUR_TOKEN_HERE
    """

    def authenticate(self, request, token: str):
        try:
            token_obj = Token.objects.select_related("user").get(key=token)
            if not token_obj.user.is_active:
                return None
            request.user = token_obj.user
            return token_obj.user
        except Token.DoesNotExist:
            return None


def get_or_create_token(user: User) -> str:
    """Get or create an API token for a user."""
    token, _ = Token.objects.get_or_create(user=user)
    return token.key
