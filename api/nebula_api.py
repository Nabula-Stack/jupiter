from ninja import NinjaAPI
from .auth import TokenAuth
from .system_routes import router as system_router
from plugins.esxi_plugin import register as register_esxi
from plugins.kvm_plugin import register as register_kvm
from plugins.proxmox_plugin import register as register_proxmox


def multi_auth(request, token_auth=TokenAuth()):
    """
    Strict multi-auth:
    1. Allow Django session-authenticated staff users.
    2. Allow valid Bearer tokens for staff users.
    3. Deny everything else.
    """
    django_request = getattr(request, "_request", request)

    # Session auth path (browser/admin).
    user = getattr(django_request, "user", None) or getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False):
        return user

    # Bearer token path (programmatic clients).
    headers = getattr(request, "headers", None) or getattr(django_request, "headers", {})
    auth_header = headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token_value = auth_header.split(" ", 1)[1].strip()
        if token_value:
            token_user = token_auth.authenticate(django_request, token_value)
            if token_user and getattr(token_user, "is_staff", False):
                request.user = token_user
                django_request.user = token_user
                return token_user

    return None


api = NinjaAPI(
    title="Jupiter API",
    version="1.5.0",
    description="Advanced API for managing ESXi Host and Network configurations via SSH",
    urls_namespace="nebula_api",
    docs_url="/docs",  # Explicitly define the docs sub-path
    auth=multi_auth,
)

# ESXi plugin — registers /hosts, /network, /storage, /vms
register_esxi(api)

# Proxmox plugin namespace — registers /proxmox/*
register_proxmox(api)

# KVM plugin namespace — registers /kvm/*
register_kvm(api)

# Core system routes (hypervisor discovery, host mapping)
api.add_router("/system", system_router, tags=["System"])
