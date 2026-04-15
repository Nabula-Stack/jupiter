"""
Django Channels consumer for handling real-time updates via WebSocket.
Manages WebSocket connections and broadcasts changes for VMs, Hosts, Networks, and Storage.

On-demand sync:
  A Redis counter ('active_sync_users') tracks how many WebSocket clients are
  connected.  A single asyncio background task runs the ESXi sync loop for as
  long as at least one client is connected, then exits automatically.
"""

import asyncio
import json

import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

# ---------------------------------------------------------------------------
#  Singleton sync-loop state  (module-level — shared across all consumer instances)
# ---------------------------------------------------------------------------

_SYNC_KEY = "active_sync_users"
_sync_task: asyncio.Task | None = None
_redis_client: aioredis.Redis | None = None

# Wrap the blocking sync cycle so it can be awaited from the event loop.
# thread_sensitive=False → runs in a regular threadpool thread, which lets
# the broadcast helpers call asyncio.run() without hitting a nested-loop error.
_run_sync_cycle = sync_to_async(
    lambda: __import__(
        "manager.services.sync_cycle", fromlist=["run_one_sync_cycle"]
    ).run_one_sync_cycle(),
    thread_sensitive=False,
)


def _get_redis() -> aioredis.Redis:
    """Return the module-level async Redis client, creating it if needed."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(host="redis", port=6379, decode_responses=True)
    return _redis_client


async def _on_demand_sync_loop() -> None:
    """
    Background asyncio task: sync ESXi every 10 s while clients are connected.

    The loop checks 'active_sync_users' before each cycle.  If the counter
    drops to 0 (all clients disconnected) the loop exits cleanly and sets
    _sync_task back to None so it can be restarted on the next connection.
    """
    global _sync_task
    r = _get_redis()
    print("[SyncLoop] Starting on-demand sync loop.")
    try:
        while True:
            raw = await r.get(_SYNC_KEY)
            if int(raw or 0) <= 0:
                print("[SyncLoop] No active clients — sync loop stopping.")
                break
            try:
                await _run_sync_cycle()
            except Exception as exc:
                print(f"[SyncLoop] Cycle error: {exc}")
            await asyncio.sleep(10)
    finally:
        _sync_task = None
        print("[SyncLoop] Sync loop exited.")


# ---------------------------------------------------------------------------
#  Consumer
# ---------------------------------------------------------------------------

class VMUpdatesConsumer(AsyncWebsocketConsumer):
    """
    Unified WebSocket consumer for all real-time infrastructure updates.

    Groups:
    - "vm_updates"      VM status and operation events
    - "host_updates"    Host configuration and status events
    - "network_updates" Network configuration events
    - "storage_updates" Storage operation events
    """

    async def connect(self):
        """
        Subscribe to all update groups, then increment the active-client
        counter and (if this is the first client) start the sync loop.
        """
        self.groups = [
            "vm_updates",
            "host_updates",
            "network_updates",
            "storage_updates",
        ]

        for group in self.groups:
            await self.channel_layer.group_add(group, self.channel_name)

        await self.accept()

        # --- On-demand sync: increment counter & start loop if needed ---
        global _sync_task
        r = _get_redis()
        count = await r.incr(_SYNC_KEY)
        print(
            f"[WebSocket] Client {self.channel_name} connected. "
            f"active_sync_users={count}"
        )

        if count == 1 and (_sync_task is None or _sync_task.done()):
            _sync_task = asyncio.create_task(_on_demand_sync_loop())
            print("[WebSocket] Sync loop task created.")

    async def disconnect(self, close_code):
        """Unsubscribe from groups and decrement the active-client counter."""
        for group in self.groups:
            await self.channel_layer.group_discard(group, self.channel_name)

        # --- On-demand sync: decrement counter (floor at 0) ---
        r = _get_redis()
        count = await r.decr(_SYNC_KEY)
        if count < 0:
            await r.set(_SYNC_KEY, 0)
            count = 0

        print(
            f"[WebSocket] Client {self.channel_name} disconnected "
            f"(code: {close_code}). active_sync_users={count}"
        )

    async def receive(self, text_data):
        """Handle ping/pong and ad-hoc group subscriptions from the client."""
        try:
            data = json.loads(text_data)
            message_type = data.get("type", "ping")

            if message_type == "ping":
                await self.send(
                    text_data=json.dumps({"type": "pong", "message": "Connection alive"})
                )
            elif message_type == "subscribe":
                group = data.get("group")
                if group and group not in self.groups:
                    await self.channel_layer.group_add(group, self.channel_name)
                    self.groups.append(group)
                    print(
                        f"[WebSocket] Client {self.channel_name} "
                        f"subscribed to {group}"
                    )

        except json.JSONDecodeError:
            print(f"[WebSocket] Invalid JSON received: {text_data}")

    # ========== VM EVENT HANDLERS ==========

    async def vm_updates_message(self, event):
        """Generic handler for all vm_updates group messages"""
        event_data = {k: v for k, v in event.items() if k != "type"}
        await self.send(text_data=json.dumps(event_data))

    async def vm_status_update(self, event):
        await self.vm_updates_message(event)

    async def vm_power_state_changed(self, event):
        await self.vm_updates_message(event)

    async def vm_created(self, event):
        await self.vm_updates_message(event)

    async def vm_modified(self, event):
        await self.vm_updates_message(event)

    async def vm_deleted(self, event):
        await self.vm_updates_message(event)

    async def vm_snapshot_created(self, event):
        await self.vm_updates_message(event)

    async def vm_snapshot_restored(self, event):
        await self.vm_updates_message(event)

    async def vm_snapshot_deleted(self, event):
        await self.vm_updates_message(event)

    async def vm_operation_started(self, event):
        await self.vm_updates_message(event)

    async def vm_operation_completed(self, event):
        await self.vm_updates_message(event)

    async def vm_operation_failed(self, event):
        await self.vm_updates_message(event)

    # ========== HOST EVENT HANDLERS ==========

    async def host_updates_message(self, event):
        """Generic handler for all host_updates group messages"""
        event_data = {k: v for k, v in event.items() if k != "type"}
        await self.send(text_data=json.dumps(event_data))

    async def host_status_update(self, event):
        await self.host_updates_message(event)

    async def host_license_updated(self, event):
        await self.host_updates_message(event)

    async def host_reboot_initiated(self, event):
        await self.host_updates_message(event)

    async def host_shutdown_initiated(self, event):
        await self.host_updates_message(event)

    # ========== NETWORK EVENT HANDLERS ==========

    async def network_updates_message(self, event):
        """Generic handler for all network_updates group messages"""
        event_data = {k: v for k, v in event.items() if k != "type"}
        await self.send(text_data=json.dumps(event_data))

    async def network_inventory_updated(self, event):
        await self.network_updates_message(event)

    async def network_portgroup_created(self, event):
        await self.network_updates_message(event)

    async def network_portgroup_deleted(self, event):
        await self.network_updates_message(event)

    async def network_vswitch_created(self, event):
        await self.network_updates_message(event)

    async def network_vswitch_deleted(self, event):
        await self.network_updates_message(event)

    # ========== STORAGE EVENT HANDLERS ==========

    async def storage_updates_message(self, event):
        """Generic handler for all storage_updates group messages"""
        event_data = {k: v for k, v in event.items() if k != "type"}
        await self.send(text_data=json.dumps(event_data))

    async def storage_inventory_updated(self, event):
        await self.storage_updates_message(event)

    async def storage_datastore_created(self, event):
        await self.storage_updates_message(event)

    async def storage_rescan_initiated(self, event):
        await self.storage_updates_message(event)

    async def storage_rescan_completed(self, event):
        await self.storage_updates_message(event)

    async def storage_directory_created(self, event):
        await self.storage_updates_message(event)

    async def storage_item_deleted(self, event):
        await self.storage_updates_message(event)
