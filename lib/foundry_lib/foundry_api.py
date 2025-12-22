import requests
from datetime import datetime

def check_players(hostname, admin_key):
    """
    Queries Foundry VTT API to check for connected players.
    Ported from check-players.sh
    """
    # Internal connections (e.g. from sidecar or cluster services) usually use HTTP
    is_internal = any(x in hostname for x in ["localhost", "127.0.0.1", ".svc.cluster.local", ".k8s.orb.local"])
    
    if is_internal:
        url = f"http://{hostname}/api/status"
    else:
        url = f"https://{hostname}/api/status"
        
    headers = {"Authorization": f"Bearer {admin_key}"}
    
    try:
        # Disable SSL verify for local connections
        verify = not (hostname.startswith("localhost") or hostname.startswith("127.0.0.1"))
        response = requests.get(url, headers=headers, timeout=10, verify=verify)
        response.raise_for_status()
        data = response.json()
        
        return {
            "connectedPlayers": data.get("users", 0),
            "worldActive": data.get("world", False),
            "checkedAt": datetime.now().isoformat()
        }
    except Exception as e:
        print(f"ERROR: Failed to connect to Foundry at {hostname}: {str(e)}")
        return {
            "connectedPlayers": -1,
            "error": "connection failed",
            "checkedAt": datetime.now().isoformat()
        }
