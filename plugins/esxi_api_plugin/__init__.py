"""ESXi API plugin exports.

This package isolates pyVmomi-powered API access for standalone ESXi hosts.
"""

from plugins.esxi_plugin.esxi_api import EsxiApiClient

__all__ = ["EsxiApiClient"]
