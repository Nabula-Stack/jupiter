from ninja import NinjaAPI

from .routes import router as kvm_router


def register(api: NinjaAPI) -> None:
    """Register KVM plugin routes under /kvm for troubleshooting and tooling."""
    api.add_router("/kvm", kvm_router, tags=["KVM Plugin"])
