from ninja import NinjaAPI

from .routes import router as proxmox_router


def register(api: NinjaAPI) -> None:
    """Register Proxmox plugin routes under /proxmox for troubleshooting and tooling."""
    api.add_router("/proxmox", proxmox_router, tags=["Proxmox Plugin"])
