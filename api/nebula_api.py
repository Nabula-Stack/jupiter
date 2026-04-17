from ninja import NinjaAPI
from ninja.security import SessionAuthIsStaff
from .system_routes import router as system_router
from plugins.esxi_plugin import register as register_esxi
from plugins.kvm_plugin import register as register_kvm
from plugins.proxmox_plugin import register as register_proxmox

api = NinjaAPI(
    title="Jupiter API",
    version="1.5.0",
    description="Advanced API for managing ESXi Host and Network configurations via SSH",
    urls_namespace="nebula_api",
    docs_url="/docs",  # Explicitly define the docs sub-path
    auth=SessionAuthIsStaff(),
)

# ESXi plugin — registers /hosts, /network, /storage, /vms
register_esxi(api)

# Proxmox plugin namespace — registers /proxmox/*
register_proxmox(api)

# KVM plugin namespace — registers /kvm/*
register_kvm(api)

# Core system routes (hypervisor discovery, host mapping)
api.add_router("/system", system_router, tags=["System"])
