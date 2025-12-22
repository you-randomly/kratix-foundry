def cleanup_for_flux(pipeline):
    """
    Removes managedFields and other fields from output manifests to make them FluxCD-compatible.
    Ported from cleanup-for-flux.sh
    """
    print("Cleaning manifests for FluxCD compatibility...")
    
    # List all files in output
    output_dir = pipeline.output_path
    for yaml_file in output_dir.glob("*.yaml"):
        with open(yaml_file, 'r') as f:
            try:
                import yaml
                data = yaml.safe_load(f)
            except Exception:
                continue # Skip invalid YAML
                
        if not data or not isinstance(data, dict):
            continue
            
        # Clean metadata recursively if it's a List or single object
        if data.get("kind") == "List":
            items = data.get("items", [])
            for item in items:
                _clean_object(item)
        else:
            _clean_object(data)
            
        # Write back
        with open(yaml_file, 'w') as f:
            yaml.dump(data, f)
            
    # CRITICAL: Remove object.yaml if it exists (Kratix default output)
    obj_path = output_dir / "object.yaml"
    if obj_path.exists():
        print(f"Removing {obj_path} (incompatible with FluxCD)")
        obj_path.unlink()

def _clean_object(obj):
    if not isinstance(obj, dict) or "metadata" not in obj:
        return
        
    metadata = obj["metadata"]
    fields_to_remove = [
        "managedFields",
        "resourceVersion",
        "uid",
        "creationTimestamp",
        "generation"
    ]
    
    for field in fields_to_remove:
        if field in metadata:
            metadata.pop(field)
