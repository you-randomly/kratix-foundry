"""
Utility for fetching and filtering FoundryVTT versions from Docker Hub.
"""

import time
import asyncio
import re
from typing import List, Tuple, Optional, Dict, Any

import aiohttp

from config import VERSION_CACHE_TTL

# Global cache
_VERSION_CACHE: Dict[str, Any] = {
    'timestamp': 0,
    'versions': [],
    'stable': None,
    'refreshing': False
}

async def get_foundry_versions() -> Tuple[List[str], Optional[str]]:
    """
    Get a list of valid FoundryVTT versions and the current stable version.
    This is non-blocking. If cache is stale, it returns stale data and triggers a refresh.
    
    Returns:
        Tuple containing:
        - List[str]: List of valid version strings
        - Optional[str]: The stable version string
    """
    global _VERSION_CACHE
    
    current_time = time.time()
    
    # If cache is valid, return it
    if _VERSION_CACHE['versions'] and (current_time - _VERSION_CACHE['timestamp'] < VERSION_CACHE_TTL):
        return _VERSION_CACHE['versions'], _VERSION_CACHE['stable']

    # If cache is populated but stale, trigger background refresh and return stale
    if _VERSION_CACHE['versions']:
        if not _VERSION_CACHE['refreshing']:
            asyncio.create_task(refresh_cache())
        return _VERSION_CACHE['versions'], _VERSION_CACHE['stable']
        
    # If cache is empty, we must wait for the refresh
    # This usually only happens on first startup if not pre-warmed
    if not _VERSION_CACHE['refreshing']:
        await refresh_cache()
        
    return _VERSION_CACHE['versions'], _VERSION_CACHE['stable']

async def refresh_cache():
    """Force a refresh of the cache."""
    global _VERSION_CACHE
    
    if _VERSION_CACHE['refreshing']:
        return
        
    _VERSION_CACHE['refreshing'] = True
    print("Refreshing Foundry versions from Docker Hub (Async)...")
    
    try:
        tags_data = await _fetch_tags_from_docker_hub()
        versions, stable = _filter_and_sort_versions(tags_data)
        
        if versions:
            _VERSION_CACHE['timestamp'] = time.time()
            _VERSION_CACHE['versions'] = versions
            _VERSION_CACHE['stable'] = stable
            print(f"Version cache updated. Stable: {stable}")
    except Exception as e:
        print(f"Error refreshing versions: {e}")
    finally:
        _VERSION_CACHE['refreshing'] = False

async def _fetch_tags_from_docker_hub() -> List[Dict]:
    """Fetch tags from Docker Hub API asynchronously."""
    url = "https://hub.docker.com/v2/repositories/felddy/foundryvtt/tags?page_size=100"
    tags_data = []
    
    # We'll fetch just a few pages to get recent versions
    max_pages = 10
    
    async with aiohttp.ClientSession() as session:
        while url and max_pages > 0:
            try:
                async with session.get(url, timeout=10) as response:
                    response.raise_for_status()
                    data = await response.json()
                    
                    for r in data.get("results", []):
                        tags_data.append({
                            "name": r["name"],
                            "digest": r["images"][0]["digest"] if r.get("images") else None
                        })
                        
                    url = data.get("next")
                    max_pages -= 1
            except Exception as e:
                print(f"Warning during tag fetch: {e}")
                break
                
    return tags_data

def _filter_and_sort_versions(tags_data: List[Dict]) -> Tuple[List[str], Optional[str]]:
    """Filter tags to find valid versions and the stable release. (CPU bound but fast enough)"""
    
    # Find 'release' tag digest if it exists
    release_digest = None
    for t in tags_data:
        if t["name"] == "release":
            release_digest = t["digest"]
            break
            
    versions_map = {} # (major, minor, patch) -> original_string
    stable_version = None
    
    # First pass: identify all valid numeric versions and populate the map
    for t in tags_data:
        name = t["name"]
        
        if not re.match(r'^\d+(\.\d+)+$', name):
            continue
            
        try:
            parts = [int(p) for p in name.split('.')]
            key_parts = list(parts)
            while len(key_parts) < 3:
                key_parts.append(0)
            semver = tuple(key_parts)
            
            if semver in versions_map:
                current = versions_map[semver]
                if len(name) < len(current):
                    versions_map[semver] = name
            else:
                versions_map[semver] = name
                
        except ValueError:
            continue

    # Second pass: Determine stable version
    if release_digest:
        best_stable_candidate = None
        for t in tags_data:
            if t["digest"] == release_digest and re.match(r'^\d+(\.\d+)+$', t["name"]):
                try:
                    parts = [int(p) for p in t["name"].split('.')]
                    key_parts = list(parts)
                    while len(key_parts) < 3:
                        key_parts.append(0)
                    semver = tuple(key_parts)
                    
                    if semver in versions_map:
                        candidate = versions_map[semver]
                        if best_stable_candidate is None or len(candidate) > len(best_stable_candidate):
                             best_stable_candidate = candidate
                except ValueError:
                    continue
        
        if best_stable_candidate:
            stable_version = best_stable_candidate

    if not versions_map:
        return [], None
        
    final_versions = list(versions_map.values())
    try:
        final_versions = sorted(
            final_versions, 
            key=lambda x: [int(p) for p in x.split('.')], 
            reverse=True
        )
    except ValueError:
        pass
    
    if not stable_version and final_versions:
        stable_version = final_versions[0]
        
    # Group by Major version and take top implementations
    majors = {}
    for t in final_versions:
        parts = t.split('.')
        try:
            major = int(parts[0])
            if major not in majors:
                majors[major] = []
            majors[major].append(t)
        except ValueError:
            continue
            
    # Get top 3 majors
    top_majors = sorted(majors.keys(), reverse=True)[:3]
    
    final_list = []
    for m in top_majors:
        final_list.extend(majors[m][:5])
        
    return final_list, stable_version

if __name__ == "__main__":
    # Test run
    # need asyncio run
    async def main():
        v, s = await get_foundry_versions()
        print(f"Stable: {s}")
        print(f"Versions: {v}")
    
    asyncio.run(main())
