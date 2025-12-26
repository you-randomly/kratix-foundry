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
