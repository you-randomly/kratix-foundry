def setup_nfs_volume(pipeline, resource: dict) -> dict:
    """
    Creates or references NFS volumes for instance data.
    Ported from setup-nfs-volume.sh
    """
    import os
    
    instance_name = resource["metadata"]["name"]
    spec = resource.get("spec", {})
    
    storage_backend = spec.get("storageBackend", "nfs")
    
    # NFS server configuration - must be set via environment variable
    nfs_server = os.environ.get("NFS_SERVER_HOST")
    if storage_backend == "nfs" and not nfs_server:
        raise ValueError(
            "NFS storage backend selected but NFS_SERVER_HOST environment variable is not set. "
            "Either set NFS_SERVER_HOST to your NFS server address, or use storageBackend: pvc"
        )
    
    nfs_base = os.environ.get("NFS_BASE_PATH", "/exports")
    
    # Determine data volume path
    data_path = spec.get("nfsBasePath")
    if not data_path or data_path == "null":
        data_path = f"{nfs_base}/instances/{instance_name}"
        
    # Paths relative to data_path
    plugin_path = f"{data_path}/Data/modules"
    world_path = f"{data_path}/Data/worlds"
    
    print("Volume paths resolved:")
    print(f"  Data: {data_path}")
    print(f"  Plugins: {plugin_path}")
    print(f"  Worlds: {world_path}")

    # Create directories on NFS if the root is mounted
    nfs_mount_root = os.environ.get("NFS_MOUNT_ROOT", "/mnt/nfs-root")
    if storage_backend == "nfs" and os.path.ismount(nfs_mount_root):
        print(f"NFS root mounted at {nfs_mount_root}, creating directories...")
        # Calculate the relative path from nfs_base to data_path
        relative_data_path = data_path.replace(nfs_base, "").lstrip("/")
        full_modules_path = os.path.join(nfs_mount_root, relative_data_path, "Data", "modules")
        full_worlds_path = os.path.join(nfs_mount_root, relative_data_path, "Data", "worlds")
        
        os.makedirs(full_modules_path, exist_ok=True)
        os.makedirs(full_worlds_path, exist_ok=True)
        print(f"  Created: {full_modules_path}")
        print(f"  Created: {full_worlds_path}")
    else:
        print(f"NFS root not mounted at {nfs_mount_root}, skipping directory creation.")

    volume_info = {
        "nfsServer": nfs_server,
        "dataPath": data_path,
        "pluginPath": plugin_path,
        "worldPath": world_path,
        "storageBackend": storage_backend
    }
    
    # Save volume info for manifest generation
    pipeline.write_metadata("volume-info.yaml", volume_info)
    
    return volume_info

