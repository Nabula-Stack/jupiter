from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class ProxmoxClient:
    host: str
    username: str
    password: str
    verify_ssl: bool = False
    timeout: int = 15

    def __post_init__(self) -> None:
        self.base_url = f"https://{self.host}:8006/api2/json"
        self._session = requests.Session()
        self._csrf_token: str | None = None
        self._authed = False

    def __enter__(self) -> "ProxmoxClient":
        self.login()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._session.close()

    def login(self) -> None:
        if self._authed:
            return
        response = self._session.post(
            f"{self.base_url}/access/ticket",
            data={"username": self.username, "password": self.password},
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        payload = response.json().get("data", {})
        ticket = payload.get("ticket")
        if not ticket:
            raise RuntimeError("Proxmox auth failed: no ticket returned")
        self._session.cookies.set("PVEAuthCookie", ticket)
        self._csrf_token = payload.get("CSRFPreventionToken")
        self._authed = True

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.login()
        headers = kwargs.pop("headers", {})
        if method.upper() in {"POST", "PUT", "DELETE"} and self._csrf_token:
            headers["CSRFPreventionToken"] = self._csrf_token
        response = self._session.request(
            method=method,
            url=f"{self.base_url}{path}",
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
            **kwargs,
        )
        if not response.ok:
            # Surface the exact Proxmox API validation details for faster debugging.
            detail = response.text.strip()
            raise requests.HTTPError(
                (
                    f"{response.status_code} {response.reason} for url: {response.url}. "
                    f"Response: {detail}"
                ),
                response=response,
            )
        if not response.text:
            return None
        body = response.json()
        return body.get("data", body)

    def create(self, path: str, data: dict[str, Any] | None = None) -> Any:
        """POST helper that mirrors proxmoxer-style create semantics."""
        return self._request("POST", path, data=data or {})

    def set(self, path: str, data: dict[str, Any] | None = None) -> Any:
        """PUT helper for update semantics used by UI action mappings."""
        return self._request("PUT", path, data=data or {})

    def delete(self, path: str, data: dict[str, Any] | None = None) -> Any:
        """DELETE helper for remove semantics used by UI action mappings.
        
        Proxmox API: DELETE requests must pass parameters in query string,
        not in request body. This method converts 'data' dict to 'params'.
        """
        if data:
            return self._request("DELETE", path, params=data)
        return self._request("DELETE", path)

    def get_nodes(self) -> list[dict[str, Any]]:
        return self._request("GET", "/nodes") or []

    def resolve_node(self, preferred_node: str | None = None) -> str:
        nodes = self.get_nodes()
        if not nodes:
            raise RuntimeError("No Proxmox nodes available")
        if preferred_node:
            for node in nodes:
                if str(node.get("node", "")).lower() == preferred_node.lower():
                    return str(node["node"])
        return str(nodes[0].get("node"))

    def get_version(self) -> dict[str, Any]:
        return self._request("GET", "/version") or {}

    def get_node_status(self, node: str) -> dict[str, Any]:
        return self._request("GET", f"/nodes/{node}/status") or {}

    def list_services(self, node: str) -> list[dict[str, Any]]:
        try:
            return self._request("GET", f"/nodes/{node}/services") or []
        except requests.RequestException:
            return []

    def list_network(self, node: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/nodes/{node}/network") or []

    def list_pci_devices(self, node: str) -> list[dict[str, Any]]:
        """List host PCI devices available on a Proxmox node."""
        try:
            return self._request("GET", f"/nodes/{node}/hardware/pci") or []
        except requests.RequestException:
            return []

    def list_storage(self, node: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/nodes/{node}/storage") or []

    def list_storage_content(self, node: str, storage: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/nodes/{node}/storage/{storage}/content") or []

    def create_storage_directory(self, node: str, storage: str) -> Any:
        return self.create(f"/nodes/{node}/storage/{storage}/content", data={"content": "rootdir"})

    def delete_storage_volume(self, node: str, storage: str, volume_id: str) -> Any:
        return self.delete(
            f"/nodes/{node}/storage/{storage}/content/{volume_id}"
        )

    def list_vms(self, node: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/nodes/{node}/qemu") or []

    def get_vm_status(self, node: str, vmid: str | int) -> dict[str, Any]:
        return self._request("GET", f"/nodes/{node}/qemu/{vmid}/status/current") or {}

    def get_vm_config(self, node: str, vmid: str | int) -> dict[str, Any]:
        return self._request("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}

    def get_agent_status(self, node: str, vmid: str | int) -> str:
        """Return KVM guest agent state as 'running' or 'not running'."""
        try:
            status_data = self.get_vm_status(node, vmid)
            if str(status_data.get("status") or "").lower() != "running":
                return "not running"

            cfg = self.get_vm_config(node, vmid)
            agent_cfg = str(cfg.get("agent") or "")
            if agent_cfg not in {"1", "enabled=1", "true", "on"} and "enabled=1" not in agent_cfg:
                return "not running"

            # This endpoint fails if qemu-guest-agent is unreachable in the guest.
            self._request("GET", f"/nodes/{node}/qemu/{vmid}/agent/ping")
            return "running"
        except Exception:
            return "not running"

    def vm_power(self, node: str, vmid: str | int, action: str) -> Any:
        # Actions: start | stop | shutdown | reset | reboot | suspend | resume
        return self.create(f"/nodes/{node}/qemu/{vmid}/status/{action}")

    def vm_create(self, node: str, data: dict[str, Any]) -> Any:
        return self.create(f"/nodes/{node}/qemu", data=data)

    def vm_update_config(self, node: str, vmid: str | int, data: dict[str, Any]) -> Any:
        return self.set(f"/nodes/{node}/qemu/{vmid}/config", data=data)

    def vm_delete(self, node: str, vmid: str | int, purge: bool = True, destroy_unreferenced_disks: bool = True) -> Any:
        """Delete a VM via the Proxmox API.
        
        Args:
            node: Proxmox node name
            vmid: VM ID (numeric)
            purge: Delete all VZDump backup files of this VM (default: True)
            destroy_unreferenced_disks: Delete unreferenced disks (default: True)
        """
        payload = {
            "purge": 1 if purge else 0,
            "destroy-unreferenced-disks": 1 if destroy_unreferenced_disks else 0,
        }
        return self.delete(f"/nodes/{node}/qemu/{vmid}", data=payload)

    def get_vnc_console_ticket(self, node: str, vmid: str | int) -> dict[str, Any]:
        """Get VNC console credentials from Proxmox API.
        Returns ticket and port for noVNC browser access.
        """
        return self._request("POST", f"/nodes/{node}/qemu/{vmid}/vncproxy") or {}

    def list_systemd_services(self, node: str) -> list[dict[str, Any]]:
        """List systemd services on a node."""
        try:
            return self._request("GET", f"/nodes/{node}/services") or []
        except requests.RequestException:
            return []

    def control_systemd_service(self, node: str, service: str, action: str) -> dict[str, Any]:
        """Control systemd service: start, stop, restart, reload, enable, disable.
        Valid actions: start | stop | restart | reload | enable | disable.
        """
        if action.lower() in {"enable", "disable"}:
            endpoint = f"/nodes/{node}/services/{service}/{action}"
            return self.create(endpoint)
        else:
            endpoint = f"/nodes/{node}/services/{service}/{action}"
            return self.create(endpoint)
