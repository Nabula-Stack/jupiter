def list_files(host, path):
    """Lists files in a specific datastore directory."""
    # Example path: /vmfs/volumes/datastore1/my_vm/
    return host.run(f"ls -lh {path}")

def delete_path(host, path):
    """Deletes a file or directory. USE WITH CAUTION."""
    return host.run(f"rm -rf {path}")

def make_directory(host, path):
    """Creates a new folder on a datastore."""
    return host.run(f"mkdir -p {path}")

def check_file_exists(host, path):
    """Returns True if the file exists on the host."""
    res = host.run(f"[ -f {path} ] && echo 'exists' || echo 'missing'")
    return res == "exists"

def move_path(host, src, dest):
    """Move/rename a file or directory."""
    return host.run(f"mv '{src}' '{dest}'")

def copy_path(host, src, dest):
    """Copy a file or directory (recursive for directories)."""
    return host.run(f"cp -r '{src}' '{dest}'")