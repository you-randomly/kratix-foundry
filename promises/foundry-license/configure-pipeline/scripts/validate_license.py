import sys

def validate_license(resource: dict) -> None:
    """
    Validates that the license secret reference exists and is properly formatted.
    Ported from validate-license.sh
    """
    license_name = resource.get("metadata", {}).get("name")
    spec = resource.get("spec", {})
    secret_ref = spec.get("licenseSecretRef", {})
    
    secret_name = secret_ref.get("name")
    secret_key = secret_ref.get("key")

    print(f"Validating FoundryLicense resource '{license_name}'...")

    if not secret_name or secret_name == "null":
        print("ERROR: licenseSecretRef.name is required", file=sys.stderr)
        sys.exit(1)

    if not secret_key or secret_key == "null":
        print("ERROR: licenseSecretRef.key is required", file=sys.stderr)
        sys.exit(1)

    print(f"License '{license_name}' validated successfully")
    print(f"  Secret: {secret_name} (key: {secret_key})")
