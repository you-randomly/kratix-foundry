#!/bin/bash
# cleanup-for-flux.sh
# Removes managedFields from object.yaml files to make them FluxCD-compatible

echo "Cleaning manifests for FluxCD compatibility..."

# Find and fix any object.yaml files in output
for file in /kratix/output/*.yaml /kratix/output/**/*.yaml; do
    if [ -f "$file" ]; then
        # Remove managedFields from any YAML file
        if grep -q "managedFields" "$file" 2>/dev/null; then
            echo "Stripping managedFields from: $file"
            yq 'del(.metadata.managedFields)' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
        fi
        # Remove resourceVersion as well (causes conflicts)
        if grep -q "resourceVersion" "$file" 2>/dev/null; then
            echo "Stripping resourceVersion from: $file"
            yq 'del(.metadata.resourceVersion)' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
        fi
        # Remove uid
        if grep -q "uid:" "$file" 2>/dev/null; then
            echo "Stripping uid from: $file"
            yq 'del(.metadata.uid)' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
        fi
        # Remove creationTimestamp
        if grep -q "creationTimestamp" "$file" 2>/dev/null; then
            echo "Stripping creationTimestamp from: $file"
            yq 'del(.metadata.creationTimestamp)' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
        fi
        # Remove generation
        if grep -q "generation:" "$file" 2>/dev/null; then
            echo "Stripping generation from: $file"
            yq 'del(.metadata.generation)' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
        fi
    fi
done

echo "Cleanup complete."

# CRITICAL: Remove object.yaml - Kratix puts this there automatically but it 
# contains managedFields which causes FluxCD to fail
if [ -f /kratix/output/object.yaml ]; then
    echo "Removing object.yaml (incompatible with FluxCD due to managedFields)"
    rm -f /kratix/output/object.yaml
fi
