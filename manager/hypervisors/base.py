from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class HypervisorAdapter(ABC):
    """Base adapter contract for hypervisor integrations.

    To add a new vendor plugin:
      1. Subclass HypervisorAdapter.
      2. Set the ``slug`` and ``display_name`` class attributes.
      3. Implement ``build_connection``, ``sync_host``, and ``sync_vms``.
      4. Call ``register_adapter(MyAdapter())`` at module import time.

    The service layer and UI are fully decoupled from vendor specifics — they
    dispatch through these three methods exclusively.
    """

    slug: str
    display_name: str

    @abstractmethod
    def build_connection(self, host: Any) -> Any:
        """Return a live connection/client object for a host record.

        The returned object must support the context-manager protocol
        (``__enter__`` / ``__exit__``) so callers can use ``with`` blocks.
        """
        raise NotImplementedError

    @abstractmethod
    def sync_host(self, host: Any, conn: Any) -> bool:
        """Pull live hardware/OS details from *conn* and persist to the Host model.

        Implementations must call ``host.save(update_fields=[...])`` and
        write data into the standard Host fields so the UI can render it
        without knowing which vendor produced the data.

        Returns True on success, False on partial or total failure.
        """
        raise NotImplementedError

    @abstractmethod
    def sync_vms(self, host: Any, conn: Any) -> int:
        """Pull the current VM inventory from *conn* and upsert into the DB.

        Implementations must write to the VirtualMachine model using the
        same field names as all other adapters so the UI remains unchanged.
        Orphaned VMs (present in DB but no longer on the hypervisor) must
        be deleted and their cache entries evicted.

        Returns the number of VMs processed.
        """
        raise NotImplementedError
