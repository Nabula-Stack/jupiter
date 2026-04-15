import importlib
import os

from .esxi_adapter import EsxiAdapter
from .kvm_adapter import KvmLibvirtAdapter
from .proxmox_adapter import ProxmoxAdapter
from .registry import get_adapter, list_adapter_slugs, register_adapter

# Register built-in adapters at import time.
register_adapter(EsxiAdapter())
register_adapter(ProxmoxAdapter())
register_adapter(KvmLibvirtAdapter())


def load_external_adapters() -> None:
	"""
	Load external adapter modules from HYPERVISOR_PLUGIN_MODULES.

	Each module can expose either:
	- register(register_adapter_callable)
	- register_adapter(register_adapter_callable)
	"""
	raw_modules = os.getenv("HYPERVISOR_PLUGIN_MODULES", "")
	modules = [module.strip() for module in raw_modules.split(",") if module.strip()]

	for module_name in modules:
		try:
			module = importlib.import_module(module_name)
			register_fn = getattr(module, "register", None) or getattr(module, "register_adapter", None)
			if callable(register_fn):
				register_fn(register_adapter)
			else:
				print(f"[HypervisorPlugin] No register function in module: {module_name}")
		except Exception as exc:
			print(f"[HypervisorPlugin] Failed to load '{module_name}': {exc}")


load_external_adapters()

__all__ = ["get_adapter", "list_adapter_slugs", "register_adapter", "load_external_adapters"]
