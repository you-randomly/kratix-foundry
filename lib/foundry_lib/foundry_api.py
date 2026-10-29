import requests
from datetime import datetime
from typing import Optional

def check_players(hostname: str, admin_key: Optional[str] = None, session: Optional[requests.Session] = None):
    """
    Queries Foundry VTT API to check for connected players and world active state.
    Ported from check-players.sh with persistent session support.
    """
    # Internal connections (e.g. from sidecar or cluster services) usually use HTTP
    is_internal = any(x in hostname for x in ["localhost", "127.0.0.1", ".svc.cluster.local", ".k8s.orb.local"])
    
    if is_internal:
        url = f"http://{hostname}/api/status"
    else:
        url = f"https://{hostname}/api/status"
        
    headers = {}
    if admin_key:
        headers["Authorization"] = f"Bearer {admin_key}"
    
    try:
        # Disable SSL verify for local connections
        verify = not (hostname.startswith("localhost") or hostname.startswith("127.0.0.1"))
        client = session if session is not None else requests
        response = client.get(url, headers=headers, timeout=10, verify=verify)
        response.raise_for_status()
        data = response.json()
        
        world = data.get("world")
        return {
            "active": data.get("active", False),
            "connectedPlayers": data.get("users", 0),
            "worldActive": bool(world),  # Convert to boolean for CRD schema
            "worldName": world if world else None,  # Keep actual world name
            "checkedAt": datetime.now().isoformat()
        }
    except Exception as e:
        print(f"ERROR: Failed to connect to Foundry at {hostname}: {str(e)}")
        return {
            "active": False,
            "connectedPlayers": -1,
            "error": "connection failed",
            "checkedAt": datetime.now().isoformat()
        }
