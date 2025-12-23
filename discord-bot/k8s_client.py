"""
Kubernetes client and CRUD operations for Foundry resources.
"""

import json
from typing import Any, Dict, List, Optional

from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from config import (
    CRD_GROUP,
    CRD_VERSION,
    CRD_INSTANCE_PLURAL,
    CRD_LICENSE_PLURAL,
    FOUNDRY_NAMESPACE,
)
from cache import instances_cache, licenses_cache, licenses_list_cache, crd_schema_cache


# Kubernetes clients (initialized on startup)
k8s_api: Optional[client.CustomObjectsApi] = None
k8s_extensions_api: Optional[client.ApiextensionsV1Api] = None


def init_kubernetes() -> bool:
    """Initialize Kubernetes client. Returns True if successful."""
    global k8s_api, k8s_extensions_api
    try:
        # Try in-cluster config first, fall back to kubeconfig
        try:
            k8s_config.load_incluster_config()
            print('Loaded in-cluster Kubernetes config')
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
            print('Loaded kubeconfig from default location')
        
        k8s_api = client.CustomObjectsApi()
        k8s_extensions_api = client.ApiextensionsV1Api()
        return True
    except Exception as e:
        print(f'WARNING: Failed to initialize Kubernetes client: {e}')
        print('Bot will run but Kubernetes commands will not work')
        return False


def is_connected() -> bool:
    """Check if Kubernetes client is initialized."""
    return k8s_api is not None


def get_foundry_instance_crd_schema() -> Dict[str, Any]:
    """Fetch the FoundryInstance CRD and extract spec properties schema.
    
    Returns dict with:
      - properties: dict of field name -> {type, enum, default, description}
    """
    if not k8s_extensions_api:
        return {'properties': {}}
    
    # Check cache
    cached = crd_schema_cache.get('foundry_instance')
    if cached is not None:
        return cached
    
    try:
        crd = k8s_extensions_api.read_custom_resource_definition(
            name='foundryinstances.foundry.platform'
        )
        # Navigate to spec.versions[0].schema.openAPIV3Schema.properties.spec.properties
        versions = crd.spec.versions
        if not versions:
            return {'properties': {}}
        
        schema = versions[0].schema
        if not schema or not schema.open_apiv3_schema:
            return {'properties': {}}
        
        spec_props = schema.open_apiv3_schema.properties.get('spec', {})
        if hasattr(spec_props, 'properties'):
            props = spec_props.properties or {}
        else:
            props = spec_props.get('properties', {})
        
        # Extract useful info from each property
        result = {'properties': {}}
        for name, prop in props.items():
            prop_info = {}
            if hasattr(prop, 'type'):
                prop_info['type'] = prop.type
            if hasattr(prop, 'enum') and prop.enum:
                prop_info['enum'] = prop.enum
            if hasattr(prop, 'default') and prop.default is not None:
                prop_info['default'] = prop.default
            if hasattr(prop, 'description'):
                prop_info['description'] = prop.description
            result['properties'][name] = prop_info
        
        crd_schema_cache.set('foundry_instance', result)
        return result
    except Exception as e:
        print(f'Error reading CRD schema: {e}')
        return {'properties': {}}


def get_storage_backend_choices() -> List[str]:
    """Get valid storageBackend enum values from CRD."""
    schema = get_foundry_instance_crd_schema()
    props = schema.get('properties', {})
    storage_prop = props.get('storageBackend', {})
    return storage_prop.get('enum', ['nfs', 'pvc'])  # Fallback if CRD read fails


def get_foundry_licenses(namespace: str = None, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Get all FoundryLicense resources.
    
    Args:
        namespace: Namespace to search in (None for cluster-wide)
        use_cache: If True, use cached results for autocomplete (5 second TTL)
    """
    if not k8s_api:
        print('get_foundry_licenses: k8s_api not initialized')
        return []
    
    # Check cache first
    if use_cache:
        cached = licenses_list_cache.get('all')
        if cached is not None:
            return cached
    
    try:
        if namespace:
            result = k8s_api.list_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural=CRD_LICENSE_PLURAL
            )
        else:
            result = k8s_api.list_cluster_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                plural=CRD_LICENSE_PLURAL
            )
        items = result.get('items', [])
        
        # Update cache
        licenses_list_cache.set('all', items)
        
        return items
    except ApiException as e:
        print(f'Error listing FoundryLicenses: {e}')
        return []


def get_foundry_license(name: str, namespace: str = None, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """Get a specific FoundryLicense by name.
    
    Args:
        name: License name
        namespace: Namespace to search in
        use_cache: If True, use cached results (5 second TTL)
    """
    if not k8s_api:
        return None
    
    # Check cache first
    if use_cache:
        cached = licenses_cache.get(name)
        if cached is not None:
            return cached
    
    ns = namespace or FOUNDRY_NAMESPACE
    try:
        result = k8s_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_LICENSE_PLURAL,
            name=name
        )
        
        # Update cache
        licenses_cache.set(name, result)
        
        return result
    except ApiException as e:
        if e.status == 404:
            return None
        print(f'Error getting FoundryLicense {name}: {e}')
        return None


def get_foundry_instances(namespace: str = None, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Get all FoundryInstance resources.
    
    Args:
        namespace: Namespace to search in (None for cluster-wide)
        use_cache: If True, use cached results for autocomplete (5 second TTL)
    """
    if not k8s_api:
        return []
    
    # Check cache first
    if use_cache:
        cached = instances_cache.get('all')
        if cached is not None:
            return cached
    
    try:
        if namespace:
            result = k8s_api.list_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=namespace,
                plural=CRD_INSTANCE_PLURAL
            )
        else:
            result = k8s_api.list_cluster_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                plural=CRD_INSTANCE_PLURAL
            )
        items = result.get('items', [])
        
        # Update cache
        instances_cache.set('all', items)
        
        return items
    except ApiException as e:
        print(f'Error listing FoundryInstances: {e}')
        return []


def get_foundry_instance(name: str, namespace: str = None) -> Optional[Dict[str, Any]]:
    """Get a specific FoundryInstance by name."""
    if not k8s_api:
        return None
    
    ns = namespace or FOUNDRY_NAMESPACE
    try:
        return k8s_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_INSTANCE_PLURAL,
            name=name
        )
    except ApiException as e:
        if e.status == 404:
            return None
        print(f'Error getting FoundryInstance {name}: {e}')
        return None


def create_foundry_instance(
    name: str,
    license_name: str,
    namespace: Optional[str] = None,
    foundry_version: Optional[str] = None,
    storage_backend: Optional[str] = None,
    cpu: Optional[str] = None,
    memory: Optional[str] = None,
    created_by_id: Optional[str] = None,
    created_by_name: Optional[str] = None
) -> Dict[str, Any]:
    """Create a FoundryInstance resource.
    
    Returns dict with 'success', 'message', and optionally 'instance'.
    """
    if not k8s_api:
        return {'success': False, 'message': 'Kubernetes not connected'}
    
    ns = namespace or FOUNDRY_NAMESPACE
    
    # Build the instance spec
    spec = {
        'licenseRef': {
            'name': license_name
        }
    }
    
    # Add optional fields only if provided
    if foundry_version:
        spec['foundryVersion'] = foundry_version
    if storage_backend:
        spec['storageBackend'] = storage_backend
    if cpu or memory:
        spec['resources'] = {}
        if cpu:
            spec['resources']['cpu'] = cpu
        if memory:
            spec['resources']['memory'] = memory
    
    # Build the full resource
    instance = {
        'apiVersion': f'{CRD_GROUP}/{CRD_VERSION}',
        'kind': 'FoundryInstance',
        'metadata': {
            'name': name,
            'namespace': ns,
            'annotations': {}
        },
        'spec': spec
    }
    
    # Add creator annotations if provided
    if created_by_id:
        instance['metadata']['annotations']['foundry.platform/created-by-id'] = created_by_id
    if created_by_name:
        instance['metadata']['annotations']['foundry.platform/created-by-name'] = created_by_name
    
    # Remove empty annotations dict
    if not instance['metadata']['annotations']:
        del instance['metadata']['annotations']
    
    try:
        result = k8s_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_INSTANCE_PLURAL,
            body=instance
        )
        return {
            'success': True,
            'message': f'Created instance "{name}" in namespace "{ns}"',
            'instance': result
        }
    except ApiException as e:
        if e.status == 409:
            return {'success': False, 'message': f'Instance "{name}" already exists'}
        elif e.status == 422:
            # Validation error - extract message
            try:
                body = json.loads(e.body)
                msg = body.get('message', str(e))
            except:
                msg = str(e.reason) if hasattr(e, 'reason') else str(e)
            return {'success': False, 'message': f'Validation error: {msg}'}
        else:
            error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
            return {'success': False, 'message': f'Failed to create instance: {error_msg}'}


def activate_instance(instance_name: str, namespace: str = None) -> Dict[str, Any]:
    """
    Activate a FoundryInstance by patching its license's activeInstanceName.
    
    Returns dict with 'success', 'message', and optionally 'license_name'.
    """
    if not k8s_api:
        return {'success': False, 'message': 'Kubernetes not connected'}
    
    ns = namespace or FOUNDRY_NAMESPACE
    
    # Get the instance to find its license
    instance = get_foundry_instance(instance_name, ns)
    if not instance:
        return {'success': False, 'message': f'Instance "{instance_name}" not found'}
    
    # Get the license name from the instance
    license_name = instance.get('spec', {}).get('licenseRef', {}).get('name')
    if not license_name:
        return {'success': False, 'message': f'Instance "{instance_name}" has no licenseRef'}
    license_obj = get_foundry_license(license_name, ns)
    if not license_obj:
        return {'success': False, 'message': f'License "{license_name}" not found'}

    # Check if already active
    is_active = license_obj.get('spec', {}).get('activeInstanceName') == instance_name
    if is_active:
        return {'success': True, 'message': f'Instance "{instance_name}" is already active', 'license_name': license_name}
    
    # Patch the license to set activeInstanceName
    try:
        patch_body = {
            'spec': {
                'activeInstanceName': instance_name
            }
        }
        k8s_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_LICENSE_PLURAL,
            name=license_name,
            body=patch_body
        )
        return {
            'success': True, 
            'message': f'Activated "{instance_name}" via license "{license_name}"',
            'license_name': license_name
        }
    except ApiException as e:
        error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
        # Check for CEL validation error (players connected + block mode)
        if e.status == 422:
            return {
                'success': False, 
                'message': f'Switch blocked: Players may be connected and switchMode is "block"',
                'license_name': license_name
            }
        return {'success': False, 'message': f'Failed to patch license: {error_msg}', 'license_name': license_name}


def deactivate_instance(instance_name: str, namespace: Optional[str] = None) -> Dict[str, Any]:
    """
    Deactivate a FoundryInstance by setting its license's activeInstanceName to None.
    
    Returns dict with 'success', 'message', and optionally 'license_name'.
    """
    if not k8s_api:
        return {'success': False, 'message': 'Kubernetes not connected'}
    
    ns = namespace or FOUNDRY_NAMESPACE
    
    # Get the instance to find its license
    instance = get_foundry_instance(instance_name, ns)
    if not instance:
        return {'success': False, 'message': f'Instance "{instance_name}" not found'}
    
    # Get the license name from the instance
    license_name = instance.get('spec', {}).get('licenseRef', {}).get('name')
    if not license_name:
        return {'success': False, 'message': f'Instance "{instance_name}" has no licenseRef'}
    license_obj = get_foundry_license(license_name, ns)
    if not license_obj:
        return {'success': False, 'message': f'License "{license_name}" not found'}

    # Check if already inactive
    current_active = license_obj.get('spec', {}).get('activeInstanceName')
    if current_active != instance_name:
        return {'success': True, 'message': f'Instance "{instance_name}" is not currently active', 'license_name': license_name}
    
    # Use merge patch with None to remove the activeInstanceName field
    try:
        patch_body = {
            'spec': {
                'activeInstanceName': None  # None/null removes the field in merge patch
            }
        }
        k8s_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_LICENSE_PLURAL,
            name=license_name,
            body=patch_body
        )
        return {
            'success': True, 
            'message': f'Deactivated "{instance_name}" - all instances now on standby',
            'license_name': license_name
        }
    except ApiException as e:
        error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
        if e.status == 422:
            # Try to extract the actual validation message
            try:
                body = json.loads(e.body)
                detail = body.get('message', error_msg)
            except:
                detail = error_msg
            return {
                'success': False, 
                'message': f'Validation error: {detail}',
                'license_name': license_name
            }
        return {'success': False, 'message': f'Failed to patch license: {error_msg}', 'license_name': license_name}


def delete_foundry_instance(name: str, namespace: str = None) -> Dict[str, Any]:
    """Delete a FoundryInstance resource.
    
    Returns dict with 'success' and 'message'.
    """
    if not k8s_api:
        return {'success': False, 'message': 'Kubernetes not connected'}
    
    ns = namespace or FOUNDRY_NAMESPACE
    try:
        k8s_api.delete_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_INSTANCE_PLURAL,
            name=name
        )
        return {'success': True, 'message': f'Deleted instance "{name}"'}
    except ApiException as e:
        if e.status == 404:
            return {'success': False, 'message': f'Instance "{name}" not found'}
        error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
        return {'success': False, 'message': f'Failed to delete instance: {error_msg}'}


def patch_instance_annotations(name: str, annotations: Dict[str, Any], namespace: str = None) -> Dict[str, Any]:
    """Patch annotations on a FoundryInstance.
    
    Args:
        name: Instance name
        annotations: Dict of annotation key -> value (use None to remove)
        namespace: Namespace
        
    Returns dict with 'success' and 'message'.
    """
    if not k8s_api:
        return {'success': False, 'message': 'Kubernetes not connected'}
    
    ns = namespace or FOUNDRY_NAMESPACE
    
    try:
        patch_body = {
            'metadata': {
                'annotations': annotations
            }
        }
        k8s_api.patch_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=ns,
            plural=CRD_INSTANCE_PLURAL,
            name=name,
            body=patch_body
        )
        return {'success': True, 'message': f'Updated annotations on "{name}"'}
    except ApiException as e:
        error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
        return {'success': False, 'message': f'Failed to patch annotations: {error_msg}'}
