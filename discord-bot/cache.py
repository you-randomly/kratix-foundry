"""
TTL cache implementation for reducing Kubernetes API server load.
"""

import time
from typing import Any, Dict, Optional

from config import CACHE_TTL_SECONDS, CRD_CACHE_TTL_SECONDS


class TTLCache:
    """Simple TTL cache for storing data with expiration."""
    
    def __init__(self, default_ttl: int = CACHE_TTL_SECONDS):
        self._data: Dict[str, Dict[str, Any]] = {}
        self._default_ttl = default_ttl
    
    def get(self, key: str, ttl: Optional[int] = None) -> Optional[Any]:
        """Get a value from cache if it exists and hasn't expired."""
        if key not in self._data:
            return None
        
        entry = self._data[key]
        age = time.time() - entry['timestamp']
        cache_ttl = ttl if ttl is not None else self._default_ttl
        
        if age < cache_ttl:
            return entry['data']
        return None
    
    def set(self, key: str, value: Any) -> None:
        """Store a value in the cache with current timestamp."""
        self._data[key] = {
            'data': value,
            'timestamp': time.time()
        }
    
    def invalidate(self, key: str) -> None:
        """Remove a key from the cache."""
        self._data.pop(key, None)
    
    def clear(self) -> None:
        """Clear all cached data."""
        self._data.clear()


# Global cache instances
instances_cache = TTLCache()
licenses_cache = TTLCache()
licenses_list_cache = TTLCache()
crd_schema_cache = TTLCache(default_ttl=CRD_CACHE_TTL_SECONDS)
