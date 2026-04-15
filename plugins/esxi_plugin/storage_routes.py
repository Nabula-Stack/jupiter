from ninja import Router, File, UploadedFile
from lib import storage
from lib.storage import manage as storage_manage
from manager.utils import get_conn
from django.views.decorators.cache import cache_page
from django.core.cache import cache
from ninja.decorators import decorate_view
from manager.utils import get_host_obj
import posixpath
from manager.models import Host
from manager.websocket_broadcaster import (
    broadcast_storage_datastore_created,
    broadcast_storage_rescan_initiated,
    broadcast_storage_rescan_completed,
    broadcast_storage_directory_created,
    broadcast_storage_item_deleted,
)

router = Router(tags=["Storage Management"])


def _get_host_obj(host_name: str):
    return get_host_obj(host_name, require_active=True)


def _is_proxmox(host_obj) -> bool:
    return host_obj.hypervisor_type == Host.HYPERVISOR_PROXMOX_VE


def _storage_root_for(host_obj) -> str:
    return "/var/lib/vz" if _is_proxmox(host_obj) else "/vmfs/volumes"


def _normalize_storage_path(host_obj, path: str) -> str:
    root = _storage_root_for(host_obj)
    normalized = posixpath.normpath(path or root)
    if _is_proxmox(host_obj):
        legacy_root = "/vmfs/volumes"
        if normalized == legacy_root or normalized.startswith(legacy_root + "/"):
            normalized = normalized.replace(legacy_root, root, 1)
    return normalized

# --- 1. DATASTORE & DISK DISCOVERY (CACHED) ---

@router.get("/{host_name}/datastores", summary="List All Datastores")
@decorate_view(cache_page(300))
def get_datastores(request, host_name: str):
    host_obj = _get_host_obj(host_name)
    data = host_obj.storage_data or {}
    return {"datastores": data.get("datastores", [])}

@router.get("/{host_name}/disks/available", summary="List Physical Disks")
@decorate_view(cache_page(600))
def get_physical_disks(request, host_name: str):
    host_obj = _get_host_obj(host_name)
    data = host_obj.storage_data or {}
    return {"raw_devices": data.get("raw_devices", "")}


# --- DATASTORE ACTIONS ---

@router.post("/{host_name}/datastores/rescan", summary="Rescan Storage Adapters")
def rescan_storage_action(request, host_name: str):
    """Triggers a full HBA and VMFS rescan on the host."""
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Storage rescan endpoint is ESXi-only. Proxmox storage is managed by node/datacenter storage configuration.",
        }

    try:
        with get_conn(host_name) as conn:
            result = storage_manage.rescan_storage(conn)
            cache.delete_pattern(f"*{host_name}/datastores*")
            return {"status": "success", "host": host_name, "output": result}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@router.post("/{host_name}/datastores/{ds_name}/unmount", summary="Unmount a Datastore")
def unmount_datastore_action(request, host_name: str, ds_name: str):
    """Safely unmounts a VMFS datastore. VMs on this datastore must be powered off first."""
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Unmount datastore endpoint is ESXi-only.",
        }

    try:
        with get_conn(host_name) as conn:
            result = storage_manage.unmount_datastore(conn, ds_name)
            cache.delete_pattern(f"*{host_name}/datastores*")
            cache.delete_pattern(f"*{host_name}/explorer*")
            return {"status": "success", "host": host_name, "datastore": ds_name, "output": (result or "").strip()}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# --- 2. FILE EXPLORER (CACHED READS) ---

@router.get("/{host_name}/explorer/ls", summary="Browse Directory Content")
@decorate_view(cache_page(60))
def browse_storage(request, host_name: str, path: str = ""):
    host_obj = _get_host_obj(host_name)
    root = _storage_root_for(host_obj)
    browse_path = _normalize_storage_path(host_obj, path or root)

    if _is_proxmox(host_obj):
        structured = browse_structured(request, host_name, browse_path)
        if structured.get("error"):
            return structured
        names = []
        for entry in structured.get("entries", []):
            suffix = "/" if entry.get("is_dir") else ""
            names.append(f"{entry.get('name', '')}{suffix}")
        return {"status": "success", "path": structured.get("path"), "entries": names}

    with get_conn(host_name) as conn:
        return storage.list_files(conn, browse_path)


@router.get("/{host_name}/explorer/browse", summary="Browse Directory (Structured JSON)")
def browse_structured(request, host_name: str, path: str = ""):
    host_obj = _get_host_obj(host_name)
    root = _storage_root_for(host_obj)
    normalized = _normalize_storage_path(host_obj, path or root)
    if not normalized.startswith(root):
        return {"error": f"Browsing is restricted to {root} (got: {normalized})"}

    if _is_proxmox(host_obj):
        with get_conn(host_name) as conn:
            node = conn.resolve_node(host_obj.name)
            entries = []
            rel = normalized[len(root):].strip("/")

            if not rel:
                storages = conn.list_storage(node)
                for ds in storages:
                    name = str(ds.get("storage") or ds.get("name") or "").strip()
                    if not name:
                        continue
                    entries.append({
                        "name": name,
                        "is_dir": True,
                        "size": "",
                        "path": f"{root}/{name}",
                    })
                entries.sort(key=lambda e: e["name"].lower())
                return {"path": normalized, "entries": entries}

            parts = rel.split("/")
            storage_name = parts[0]
            try:
                content_rows = conn.list_storage_content(node, storage_name)
            except Exception as exc:
                return {"error": f"Failed to list Proxmox storage '{storage_name}': {exc}"}

            for row in content_rows:
                volid = str(row.get("volid") or "")
                if not volid:
                    continue
                # Example volid: local:iso/file.iso, local-lvm:vm-100-disk-0
                vol_part = volid.split(":", 1)[1] if ":" in volid else volid
                display_name = vol_part.split("/")[-1] if "/" in vol_part else vol_part
                entries.append({
                    "name": display_name,
                    "is_dir": False,
                    "size": str(row.get("size") or ""),
                    "path": f"{root}/{storage_name}/{display_name}",
                    "volid": volid,
                    "kind": row.get("content") or row.get("format") or "volume",
                })
            entries.sort(key=lambda e: e["name"].lower())
            return {"path": normalized, "entries": entries}

    with get_conn(host_name) as conn:
        browse_path = normalized.rstrip("/") + "/"
        raw = conn.run(f"ls -lhLp {browse_path}")
        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("total") or line.endswith(":"):
                continue
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            raw_name = parts[8]
            if " -> " in raw_name:
                name = raw_name.split(" -> ")[0]
                is_dir = parts[0].startswith("l") or parts[0].startswith("d")
            else:
                name = raw_name
                is_dir = name.endswith("/")
            name = name.rstrip("/")
            size = parts[4] if not is_dir else ""
            entries.append({
                "name": name,
                "is_dir": is_dir,
                "size": size,
                "path": f"{normalized.rstrip('/')}/{name}",
            })
        return {"path": normalized, "entries": entries}

@router.get("/{host_name}/explorer/exists", summary="Verify Path Existence")
@decorate_view(cache_page(60))
def check_path(request, host_name: str, path: str):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        root = _storage_root_for(host_obj)
        normalized = _normalize_storage_path(host_obj, path or root)
        if normalized == root:
            return {"exists": True, "path": normalized}

        with get_conn(host_name) as conn:
            node = conn.resolve_node(host_obj.name)
            rel = normalized[len(root):].strip("/") if normalized.startswith(root) else ""
            if not rel:
                return {"exists": True, "path": normalized}

            parts = rel.split("/")
            storage_name = parts[0]
            if len(parts) == 1:
                storages = conn.list_storage(node)
                exists = any(str(ds.get("storage") or ds.get("name") or "") == storage_name for ds in storages)
                return {"exists": exists, "path": normalized}

            content_rows = conn.list_storage_content(node, storage_name)
            target_name = parts[-1]
            exists = any(
                (str(row.get("volid") or "").split(":", 1)[-1].split("/")[-1] == target_name)
                for row in content_rows
            )
            return {"exists": exists, "path": normalized}

    with get_conn(host_name) as conn:
        return storage.check_file_exists(conn, path)


# --- 3. MANAGEMENT (IMMEDIATE + BUSTING) ---

@router.post("/{host_name}/explorer/mkdir", summary="Create New Directory")
def make_dir(request, host_name: str, path: str):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Directory creation is not supported for Proxmox file browser paths. Use Proxmox storage content APIs.",
        }
    try:
        with get_conn(host_name) as conn:
            result = storage.make_directory(conn, path)
            cache.delete_pattern(f"*{host_name}/explorer/ls*")
            broadcast_storage_directory_created(host_name, path)
            return {"status": "success", "output": result.strip()}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/{host_name}/explorer/rm", summary="Delete File or Folder")
def delete_item(request, host_name: str, path: str):
    host_obj = _get_host_obj(host_name)
    try:
        normalized = _normalize_storage_path(host_obj, path)
        if _is_proxmox(host_obj):
            root = _storage_root_for(host_obj)
            if not normalized.startswith(root):
                return {"status": "error", "message": f"Delete restricted to {root}"}

            rel = normalized[len(root):].strip("/")
            if not rel or "/" not in rel:
                return {"status": "error", "message": "Cannot delete Proxmox storage root or storage namespace."}

            storage_name, volume_name = rel.split("/", 1)
            with get_conn(host_name) as conn:
                node = conn.resolve_node(host_obj.name)
                content_rows = conn.list_storage_content(node, storage_name)
                volid = None
                for row in content_rows:
                    candidate = str(row.get("volid") or "")
                    if candidate.split(":", 1)[-1].split("/")[-1] == volume_name:
                        volid = candidate
                        break
                if not volid:
                    return {"status": "error", "message": f"Volume '{volume_name}' not found in storage '{storage_name}'"}
                conn.delete_storage_volume(node, storage_name, volid)
                cache.delete_pattern(f"*{host_name}/explorer*")
                broadcast_storage_item_deleted(host_name, normalized)
                return {"status": "item_deleted", "path": normalized}

        if not normalized.startswith("/vmfs/volumes"):
            return {"status": "error", "message": "Delete restricted to /vmfs/volumes"}
        with get_conn(host_name) as conn:
            result = storage.delete_path(conn, normalized)
            cache.delete_pattern(f"*{host_name}/explorer*")
            broadcast_storage_item_deleted(host_name, normalized)
            return {"status": "item_deleted", "path": normalized}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/explorer/move", summary="Move / Rename File or Folder")
def move_item(request, host_name: str, src: str, dest: str):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Move/rename is not supported in Proxmox file browser mode.",
        }
    try:
        norm_src = posixpath.normpath(src)
        norm_dest = posixpath.normpath(dest)
        if not norm_src.startswith("/vmfs/volumes") or not norm_dest.startswith("/vmfs/volumes"):
            return {"status": "error", "message": "Paths must be under /vmfs/volumes"}
        with get_conn(host_name) as conn:
            result = storage.move_path(conn, norm_src, norm_dest)
            if isinstance(result, str) and result.startswith("Error:"):
                return {"status": "error", "message": result}
            cache.delete_pattern(f"*{host_name}/explorer*")
            return {"status": "success", "src": norm_src, "dest": norm_dest}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/explorer/copy", summary="Copy File or Folder")
def copy_item(request, host_name: str, src: str, dest: str):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Copy is not supported in Proxmox file browser mode.",
        }
    try:
        norm_src = posixpath.normpath(src)
        norm_dest = posixpath.normpath(dest)
        if not norm_src.startswith("/vmfs/volumes") or not norm_dest.startswith("/vmfs/volumes"):
            return {"status": "error", "message": "Paths must be under /vmfs/volumes"}
        with get_conn(host_name) as conn:
            result = storage.copy_path(conn, norm_src, norm_dest)
            if isinstance(result, str) and result.startswith("Error:"):
                return {"status": "error", "message": result}
            cache.delete_pattern(f"*{host_name}/explorer*")
            return {"status": "success", "src": norm_src, "dest": norm_dest}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/rescan", summary="Trigger Storage Rescan")
def rescan_adapters(request, host_name: str):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Adapter rescan endpoint is ESXi-only.",
        }

    try:
        with get_conn(host_name) as conn:
            broadcast_storage_rescan_initiated(host_name)
            msg = storage.rescan_storage(conn)
            storage.refresh_vmfs(conn)
            cache.delete_pattern(f"*{host_name}*")
            broadcast_storage_rescan_completed(host_name)
            return {"message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/datastore/create", summary="Format and Create Datastore")
def create_new_ds(request, host_name: str, disk_id: str, ds_name: str):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Datastore create endpoint is ESXi-only. Add storage from Proxmox node/datacenter settings.",
        }

    try:
        with get_conn(host_name) as conn:
            result = storage.create_datastore(conn, disk_id, ds_name)
            cache.delete_pattern(f"*{host_name}*")
            broadcast_storage_datastore_created(host_name, ds_name)
            return {"status": "process_complete", "output": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/{host_name}/explorer/upload", summary="Upload File to Datastore")
def upload_file(request, host_name: str, path: str, file: UploadedFile = File(...)):
    host_obj = _get_host_obj(host_name)
    if _is_proxmox(host_obj):
        return {
            "status": "error",
            "message": "Direct upload to Proxmox browser path is not supported by this endpoint.",
        }

    normalized = _normalize_storage_path(host_obj, path)
    if not normalized.startswith("/vmfs/volumes"):
        return {"status": "error", "message": "Uploads restricted to /vmfs/volumes"}

    filename = posixpath.basename(file.name or "upload")
    if not filename or "/" in filename or "\\" in filename or "\x00" in filename:
        return {"status": "error", "message": "Invalid filename"}

    remote_path = f"{normalized.rstrip('/')}/{filename}"
    try:
        with get_conn(host_name) as conn:
            conn.upload_file(file, remote_path)
            cache.delete_pattern(f"*{host_name}/explorer*")
            return {"status": "success", "path": remote_path, "filename": filename}
    except Exception as e:
        return {"status": "error", "message": str(e)}
