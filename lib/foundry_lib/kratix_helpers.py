import os
import yaml
import json
from pathlib import Path

class Pipeline:
    """Helper class to interact with Kratix pipeline I/O conventions."""
    
    def __init__(self, input_path="/kratix/input", output_path="/kratix/output", metadata_path="/kratix/metadata"):
        self.input_path = Path(input_path)
        self.output_path = Path(output_path)
        self.metadata_path = Path(metadata_path)
        
        # Ensure directories exist
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.metadata_path.mkdir(parents=True, exist_ok=True)

    def resource(self) -> dict:
        """Read the main resource object from Kratix input."""
        with open(self.input_path / "object.yaml", 'r') as f:
            return yaml.safe_load(f)

    def write_output(self, filename: str, content: dict):
        """Write a manifest to the Kratix output directory."""
        with open(self.output_path / filename, 'w') as f:
            yaml.dump(content, f)

    def write_status(self, status: dict):
        """Update the resource status via Kratix metadata."""
        with open(self.metadata_path / "status.yaml", 'w') as f:
            yaml.dump(status, f)

    def metadata(self, filename: str) -> dict:
        """Read a file from the Kratix metadata directory (if it exists)."""
        path = self.metadata_path / filename
        if not path.exists():
            return {}
        with open(path, 'r') as f:
            return yaml.safe_load(f)

    def write_metadata(self, filename: str, content: dict):
        """Write a file to the Kratix metadata directory."""
        with open(self.metadata_path / filename, 'w') as f:
            yaml.dump(content, f)
