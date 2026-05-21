# manager/services/service_host.py
#
# Thin dispatcher — vendor-specific sync logic lives in each HypervisorAdapter.
# To support a new hypervisor, implement HypervisorAdapter.sync_host() in a new
# adapter class and register it.  No changes are ever needed here or in the UI.

from manager.hypervisors import get_adapter
from manager.utils import get_conn


def sync_host_details_to_db(host, conn=None) -> bool:
    """Sync host hardware/OS details via the registered hypervisor adapter.

    If *conn* is provided (shared-session mode from run_sync.py) it is passed
    directly to the adapter.  Otherwise a fresh connection is opened.
    """
    adapter = get_adapter(host.hypervisor_type)
    try:
        if conn is not None:
            return adapter.sync_host(host, conn)
        with get_conn(host.name) as fresh_conn:
            return adapter.sync_host(host, fresh_conn)
    except Exception as exc:
        print(f"❌ sync_host_details_to_db failed for '{host.name}': {exc}")
        return False