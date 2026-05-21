"""ESXi SSH plugin helpers.

This package isolates SSH-only connection building so ESXi SSH and API
implementations can evolve independently while preserving existing UI routes.
"""

from __future__ import annotations

import os
from typing import Any

from lib.connect.connect import ESXiConnect


def build_esxi_ssh_connection(host_obj: Any) -> ESXiConnect:
    """Create an SSH connection object for an ESXi host."""
    ssh_key_path = (
        os.getenv("ESXI_SSH_KEY_PATH")
        or os.getenv("SSH_KEY_PATH")
        or os.getenv("SSH_KEY_CONTAINER_PATH")
        or ("/app/nebula_rsa" if os.path.exists("/app/nebula_rsa") else None)
    )
    ssh_key_passphrase = os.getenv("ESXI_SSH_KEY_PASSPHRASE") or os.getenv("SSH_KEY_PASSPHRASE")
    return ESXiConnect(
        host=host_obj.ip_address,
        user=host_obj.username,
        key_filename=ssh_key_path,
        key_passphrase=ssh_key_passphrase,
    )
