# lib/host/__init__.py

# 1. Read/Info functions now come from .info
from .info import (
    get_host_summary, 
    get_host_hardware,
    get_host_usage_stats,
    get_license_details,  # Note: Ensure this matches the name in info.py
    get_host_runtime
)

# 2. Action/Write functions come from .manage
from .manage import (
    add_license, 
    reboot_host, 
    shutdown_host, 
    set_maintenance_mode
)

