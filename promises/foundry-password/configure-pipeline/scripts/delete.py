#!/usr/bin/env python3
"""
FoundryPassword Pipeline - Delete handler.

Cleans up resources when a FoundryPassword is deleted.
The ExternalSecret and resulting Secret will be cleaned up by Kratix/Flux
as part of the normal resource deletion process.
"""
import sys

sys.path.append("/app")

from foundry_lib.kratix_helpers import Pipeline


def main():
    try:
        pipeline = Pipeline()
        resource = pipeline.resource()
        
        password_name = resource["metadata"]["name"]
        print(f"FoundryPassword {password_name} deletion initiated.")
        print("ExternalSecret and Secret cleanup handled by Kratix/Flux.")
        
        # No explicit cleanup needed - Kratix removes outputs when resource is deleted
        
    except Exception as e:
        print(f"ERROR during cleanup: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        # Don't fail deletion on errors
        sys.exit(0)


if __name__ == "__main__":
    main()
