# Hypervisor Plugin Architecture and Standards

This project can now resolve a hypervisor adapter per host using `Host.hypervisor_type`.
The built-in adapter is `vmware_esxi`.

## Goal

Keep API and web behavior stable while adding support for additional hypervisors
(Hyper-V, Proxmox, AHV, KVM, custom vendors).

## Current Plugin Entry Points

- Adapter contract: `manager/hypervisors/base.py`
- Adapter registry: `manager/hypervisors/registry.py`
- Built-in ESXi adapter: `manager/hypervisors/esxi_adapter.py`
- Connection resolver: `manager/utils.py`
- External plugin loader env var: `HYPERVISOR_PLUGIN_MODULES`

## API Standardization Rules

1. Keep all public API endpoints versioned under `/api/v1/`.
2. Use resource-based route groups (`/hosts`, `/network`, `/storage`, `/vms`, `/system`).
3. Keep route handlers thin and delegate vendor-specific behavior to adapter/service layers.
4. Use stable response keys for web clients even when backend vendor differs.
5. Add capability endpoints for feature discovery (`/api/v1/system/hypervisors`).

## Web/Admin Standardization Rules

1. Web pages and admin views must not assume ESXi-only labels in user-facing text.
2. Every host row should display its `hypervisor_type`.
3. UI actions should call API endpoints, not vendor CLI directly from templates.
4. Keep per-vendor branching out of templates; branch in services/adapters.

## How to Add a New Hypervisor Plugin

1. Create a new adapter class in `manager/hypervisors/` implementing `HypervisorAdapter`.
2. Register it in `manager/hypervisors/__init__.py` with `register_adapter(...)`.
3. Add any vendor-specific service wrappers for host/network/storage/vm operations.
4. Add integration tests against a mock or sandbox target.
5. Set host records to the new `hypervisor_type` in admin.

### External package registration

You can ship plugins in separate Python packages and set:

`HYPERVISOR_PLUGIN_MODULES=my_vendor.plugin,other_vendor.plugin`

Each module should expose one of:

- `register(register_adapter)`
- `register_adapter(register_adapter)`

Inside that function, call `register_adapter(YourAdapter())`.

## Recommended Next Step (Service Layer)

Create explicit service interfaces for:

- host summary and lifecycle
- vm inventory and power operations
- network inventory and changes
- storage inventory and file operations

Then route handlers call those interfaces, and adapters provide vendor-specific implementations.
