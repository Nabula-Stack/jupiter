from __future__ import annotations

from typing import Dict

from .base import HypervisorAdapter


_ADAPTERS: Dict[str, HypervisorAdapter] = {}


def register_adapter(adapter: HypervisorAdapter) -> None:
    _ADAPTERS[adapter.slug] = adapter


def get_adapter(slug: str) -> HypervisorAdapter:
    if slug not in _ADAPTERS:
        supported = ", ".join(sorted(_ADAPTERS.keys()))
        raise ValueError(f"Unsupported hypervisor type '{slug}'. Supported: {supported}")
    return _ADAPTERS[slug]


def list_adapter_slugs() -> list[str]:
    return sorted(_ADAPTERS.keys())
