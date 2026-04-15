"""
Single-cycle sync entry point used by the on-demand WebSocket sync loop
(manager/consumers.py).  Each call fetches all active hosts and syncs
their metadata + VMs in parallel using one SSH session per host.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import close_old_connections, OperationalError

from manager.models import Host, VirtualMachine
from manager.services.service_host import sync_host_details_to_db
from manager.services.service_vm import sync_vms_for_host
from manager.websocket_service import broadcast_vm_batch
from manager.host_broadcast_service import broadcast_host_batch
from manager.utils import get_conn


def _fetch_active_hosts(max_retries: int = 3, delay_seconds: int = 3) -> list:
    for attempt in range(1, max_retries + 1):
        try:
            close_old_connections()
            return list(Host.objects.filter(is_active=True))
        except OperationalError as exc:
            print(f"[SyncCycle] DB error fetching hosts (attempt {attempt}/{max_retries}): {exc}")
            if attempt < max_retries:
                time.sleep(delay_seconds)
        finally:
            close_old_connections()
    return []


def _sync_host_worker(host) -> dict:
    close_old_connections()
    try:
        with get_conn(host.name) as conn:
            host_ok = sync_host_details_to_db(host, conn=conn)
            vm_count = sync_vms_for_host(host, conn=conn) or 0

        close_old_connections()
        vms_updated = VirtualMachine.objects.filter(host=host)
        if vms_updated.exists():
            broadcast_vm_batch(vms_updated)

        return {
            "host": host.name,
            "status": "success",
            "host_ok": host_ok,
            "vm_count": vm_count,
            "error": None,
        }
    except Exception as exc:
        return {
            "host": host.name,
            "status": "failed",
            "host_ok": False,
            "vm_count": 0,
            "error": str(exc),
        }
    finally:
        close_old_connections()


def run_one_sync_cycle() -> None:
    """Sync all active hosts once.  Called every ~5 s by the WebSocket sync loop."""
    hosts = _fetch_active_hosts()
    if not hosts:
        return

    max_workers = min(len(hosts), os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_sync_host_worker, host): host for host in hosts}
        for future in as_completed(futures, timeout=180):
            try:
                result = future.result()
                if result["status"] != "success":
                    print(f"[SyncCycle] Failed [{result['host']}]: {result['error']}")
            except Exception as exc:
                print(f"[SyncCycle] Worker exception: {exc}")
