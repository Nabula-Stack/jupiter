import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


class EncryptedTextField(models.TextField):
    """Encrypt text at rest while returning plaintext in Python."""

    _prefix = "enc::"

    def _build_fernet(self) -> Fernet:
        configured_key = getattr(settings, "SSH_PUBLIC_KEY_ENCRYPTION_KEY", "") or settings.SECRET_KEY
        key_bytes = configured_key.encode("utf-8")
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest())
        return Fernet(fernet_key)

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return value

        if isinstance(value, str) and value.startswith(self._prefix):
            token = value[len(self._prefix):]
            try:
                return self._build_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
            except (InvalidToken, ValueError, TypeError):
                # Keep raw value to avoid hard-failing reads if key changes unexpectedly.
                return value

        # Backward compatibility for legacy plaintext records.
        return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value

        if isinstance(value, str) and value.startswith(self._prefix):
            return value

        encrypted = self._build_fernet().encrypt(str(value).encode("utf-8")).decode("utf-8")
        return f"{self._prefix}{encrypted}"
