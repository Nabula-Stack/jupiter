import os
import sys
import django
import time
import json
import traceback
from django.core.cache import cache
from django.db import OperationalError
from django.db import close_old_connections
from concurrent.futures import ThreadPoolExecutor, as_completed

# 1. Path Setup
CURRENT_DIR = os.getcwd() 
if CURRENT_DIR not in sys.path:
    sys.path.append(CURRENT_DIR)

# 2. Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from manager.services.service_host import sync_host_details_to_db
from manager.services.service_vm import sync_vms_for_host
from manager.models import VirtualMachine, Host
from manager.websocket_service import broadcast_vm_batch
from manager.host_broadcast_service import broadcast_host_batch
from manager.utils import get_conn

def fetch_active_hosts(max_retries=3, delay_seconds=3):
    """Fetch active hosts with retry to survive transient DB timeouts."""
    for attempt in range(1, max_retries + 1):
        try:
            close_old_connections()
            return list(Host.objects.filter(is_active=True))
        except OperationalError as e:
            print(
                f"[{time.strftime('%H:%M:%S')}] ⚠️ DB timeout fetching hosts "
                f"(attempt {attempt}/{max_retries}): {e}"
            )
            if attempt < max_retries:
                time.sleep(delay_seconds)
        finally:
            close_old_connections()
    return []

def sync_host_and_vms_worker(host):
    """Sync host metadata and VMs using a single SSH session per host cycle."""
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
            "host_status": "success" if host_ok else "failed",
            "vm_count": vm_count,
            "status": "success",
            "error": None,
        }
    except OperationalError as e:
        return {
            "host": host.name,
            "host_status": "failed",
            "vm_count": 0,
            "status": "failed",
            "error": f"DB error: {e}",
        }
    except Exception as e:
        return {
            "host": host.name,
            "host_status": "failed",
            "vm_count": 0,
            "status": "failed",
            "error": str(e),
        }
    finally:
        close_old_connections()

def debug_cache_state():
    """Checks the cache specifically for Networking and DNS data."""
    try:
        sample_vm = VirtualMachine.objects.filter(name__icontains="rancher").first() or VirtualMachine.objects.first()
    except OperationalError as e:
        print(f"   ⚠️ Cache debug skipped due to DB error: {e}")
        return
    
    if sample_vm:
        key = f"ninja:vm_details:{sample_vm.host.ip_address}:{sample_vm.vmid}"
        data = cache.get(key)
        
        # Handle cases where cache might be stored as a string
        if isinstance(data, str):
            try: data = json.loads(data)
            except: pass

        if data:
            print(f"   🔍 DEBUG: Cache lookup for '{sample_vm.name}' [{sample_vm.vmid}] successful.")
            
            # Check Networks
            nets = data.get('networks', [])
            print(f"   📡 Networks: {len(nets)} interfaces found.")
            
            # Check DNS
            dns = data.get('dns_servers', [])
            if dns:
                print(f"   ✅ DNS FOUND: {dns}")
            else:
                print(f"   ⚠️  DNS MISSING: 'dns_servers' key is empty or missing in cache.")
        else:
            print(f"   ❌ DEBUG: Cache key '{key}' is EMPTY. Sync might not be saving to cache.")

def start_worker():
    print("🚀 ESXi Background Sync Worker Started (SINGLE SSH SESSION PER HOST MODE)...")
    print("--- Press Ctrl+C to stop ---")

    auth_failures = {}
    auth_retry_after = {}

    def is_auth_failure(error_text):
        if not isinstance(error_text, str):
            return False
        # ESXi SSH auth failure
        if "Authentication failed" in error_text:
            return True
        # Proxmox REST API auth failure (requests raises HTTPError with status)
        if "401" in error_text and ("Unauthorized" in error_text or "Client Error" in error_text):
            return True
        # Proxmox ticket missing — raised by ProxmoxClient.login()
        if "Proxmox auth failed" in error_text:
            return True
        return False

    def record_auth_failure(host_name):
        failures = auth_failures.get(host_name, 0) + 1
        auth_failures[host_name] = failures
        # Exponential backoff: 15s, 30s, 60s... capped at 5 minutes.
        cooldown = min(300, 15 * (2 ** (failures - 1)))
        auth_retry_after[host_name] = time.time() + cooldown
        return failures, cooldown

    def clear_auth_failure(host_name):
        auth_failures.pop(host_name, None)
        auth_retry_after.pop(host_name, None)
    
    while True:
        try:
            start_time = time.time()
            print(f"\n[{time.strftime('%H:%M:%S')}] 🔄 Syncing hosts & VMs in parallel...")
            
            # Get all active hosts (with retry)
            hosts = fetch_active_hosts(max_retries=3, delay_seconds=3)

            if not hosts:
                print(f"[{time.strftime('%H:%M:%S')}] ⚠️  No active hosts to sync.")
                print(f"[{time.strftime('%H:%M:%S')}] ⏳ Waiting 30s until next sync...\n")
                time.sleep(30)
                continue
            
            # Skip hosts currently in auth-cooldown window to avoid lockouts.
            now = time.time()
            skipped_hosts = []
            runnable_hosts = []
            for host in hosts:
                retry_at = auth_retry_after.get(host.name, 0)
                if retry_at > now:
                    remaining = int(retry_at - now)
                    skipped_hosts.append((host.name, remaining))
                    continue
                runnable_hosts.append(host)

            if skipped_hosts:
                skip_text = ", ".join([f"{name} ({secs}s)" for name, secs in skipped_hosts])
                print(f"   ⏭️ Auth cooldown active, skipping: {skip_text}")

            if not runnable_hosts:
                print(f"[{time.strftime('%H:%M:%S')}] ⏳ All hosts in cooldown. Waiting 5s until next sync...\n")
                time.sleep(5)
                continue

            # Run per-host syncs in parallel, each host reuses one SSH session for host+VM sync.
            host_results = []
            vm_results = []
            total_vms = 0
            failed_hosts = set()
            
            with ThreadPoolExecutor(max_workers=min(len(runnable_hosts), os.cpu_count() or 4)) as executor:
                futures = {executor.submit(sync_host_and_vms_worker, host): host for host in runnable_hosts}

                print("   📊 Syncing Host Metadata + VMs (shared SSH session per host):")
                for future in as_completed(futures, timeout=180):
                    try:
                        result = future.result()
                        if result["status"] == "success":
                            print(f"      ✅ [{result['host']}] ({result['vm_count']} VMs)")
                            clear_auth_failure(result["host"])
                            host_results.append(result)
                            vm_results.append(result)
                            total_vms += result['vm_count']
                        else:
                            print(f"      ❌ [{result['host']}]: {result['error']}")
                            if is_auth_failure(result.get("error")):
                                failures, cooldown = record_auth_failure(result["host"])
                                print(
                                    f"         🔐 Auth backoff [{result['host']}] "
                                    f"failure #{failures}, retry in {cooldown}s"
                                )
                            failed_hosts.add(result['host'])
                    except Exception as e:
                        host_name = futures[future].name
                        print(f"      ❌ [{host_name}]: {e}")
                        if is_auth_failure(str(e)):
                            failures, cooldown = record_auth_failure(host_name)
                            print(
                                f"         🔐 Auth backoff [{host_name}] "
                                f"failure #{failures}, retry in {cooldown}s"
                            )
                        failed_hosts.add(host_name)

            # 🔴 Broadcast host updates to WebSocket clients
            try:
                successful_hosts = [r["host"] for r in host_results]
                hosts_updated = Host.objects.filter(is_active=True, name__in=successful_hosts)
                if hosts_updated.exists():
                    print(f"   🌐 Broadcasting host updates ({hosts_updated.count()} hosts)...")
                    broadcast_host_batch(hosts_updated)
            except OperationalError as e:
                print(f"   ⚠️ Skipped host broadcast due to DB error: {e}")
            
            # Verify cache
            debug_cache_state()
            
            # Summary
            if failed_hosts:
                print(f"   ⚠️  Failed hosts: {', '.join(failed_hosts)}")
            
            duration = round(time.time() - start_time, 2)
            print(f"[{time.strftime('%H:%M:%S')}] ✅ Sync Complete ({duration}s | {len(host_results)} hosts | {total_vms} VMs)")
            
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ❌ Global Sync Error: {e}")
            traceback.print_exc() 
        
        # Sleep for 5 seconds before next sync
        print(f"[{time.strftime('%H:%M:%S')}] ⏳ Waiting 5s until next sync...\n")
        time.sleep(5)

if __name__ == "__main__":
    try:
        start_worker()
    except KeyboardInterrupt:
        print("\n\n👋 Sync worker stopped by user.")
        sys.exit(0)