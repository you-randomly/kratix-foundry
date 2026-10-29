"""
Network and activity detection for Foundry VTT instances.
Pure logic module with zero Kubernetes dependencies for testability.
"""
from dataclasses import dataclass, field
import ipaddress
import re
from typing import Dict, List, Optional, Tuple

@dataclass
class ConnectionInfo:
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    state: str = "ESTAB"
    bytes_received: int = 0
    bytes_sent: int = 0
    lastrcv: int = 0  # ms since last received packet

def normalize_ip(ip_str: str) -> str:
    """Normalize IPv4-mapped IPv6 addresses like [::ffff:10.244.2.26] to 10.244.2.26."""
    s = ip_str.strip("[]")
    if s.lower().startswith("::ffff:"):
        s = s[7:]
    return s

def is_public_ip(ip_str: str) -> bool:
    """Check if an IP address is a public, non-loopback, non-private IP."""
    clean_ip = normalize_ip(ip_str)
    try:
        ip = ipaddress.ip_address(clean_ip)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return False

def parse_endpoint(endpoint_str: str) -> Tuple[str, int]:
    """Parse endpoint string like '[::ffff:10.244.3.24]:30000' or '10.244.3.24:30000'."""
    s = endpoint_str.strip()
    if s.startswith("[") and "]:" in s:
        ip_part, port_part = s.split("]:")
        return normalize_ip(ip_part[1:]), int(port_part)
    if ":" in s:
        parts = s.rsplit(":", 1)
        return normalize_ip(parts[0]), int(parts[1])
    return s, 0

def parse_ss(output: str) -> List[ConnectionInfo]:
    """
    Parse ss output (from ss -tinH state established, or combined with ss -tanH).
    Handles multi-line output where line 1 has connection endpoints and line 2 has tcp_info.
    """
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    connections: List[ConnectionInfo] = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        tokens = line.split()
        if not tokens or line.startswith("\t") or line.startswith("   ") or line.startswith("==="):
            i += 1
            continue
            
        addr_indices = [idx for idx, t in enumerate(tokens) if ":" in t]
        if len(addr_indices) >= 2:
            local_str = tokens[addr_indices[0]]
            remote_str = tokens[addr_indices[1]]
            local_ip, local_port = parse_endpoint(local_str)
            remote_ip, remote_port = parse_endpoint(remote_str)
            
            state = "ESTAB"
            for token in tokens:
                if token in ("TIME-WAIT", "CLOSE-WAIT", "SYN-SENT", "LISTEN", "ESTAB"):
                    state = token
                    break
                
            conn = ConnectionInfo(
                local_ip=local_ip,
                local_port=local_port,
                remote_ip=remote_ip,
                remote_port=remote_port,
                state=state
            )
            
            # Check next line for tcp_info if indented
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if next_line.startswith("\t") or next_line.startswith("   "):
                    m_br = re.search(r"bytes_received:(\d+)", next_line)
                    if m_br:
                        conn.bytes_received = int(m_br.group(1))
                    m_bs = re.search(r"bytes_sent:(\d+)", next_line)
                    if m_bs:
                        conn.bytes_sent = int(m_bs.group(1))
                    m_lr = re.search(r"lastrcv:(\d+)", next_line)
                    if m_lr:
                        conn.lastrcv = int(m_lr.group(1))
                    i += 1
                    
            connections.append(conn)
        i += 1
        
    return connections

class SessionTracker:
    """Tracks inbound browser connections to port 30000 over time."""
    def __init__(self, idle_threshold_seconds: float = 300.0, max_heartbeat_rate_bps: float = 1.0):
        self.idle_threshold_seconds = idle_threshold_seconds
        self.max_heartbeat_rate_bps = max_heartbeat_rate_bps  # 1.0 byte/s max for heartbeat
        # Map of (remote_ip, remote_port) -> {first_seen, last_seen, samples: list of (time, bytes_received)}
        self.sessions: Dict[Tuple[str, int], Dict] = {}

    def update(self, inbound_conns: List[ConnectionInfo], now: float) -> Tuple[int, int]:
        """
        Update tracker with current inbound connections.
        Returns: (active_count, idle_count)
        """
        current_keys = set()
        for conn in inbound_conns:
            if conn.local_port != 30000 or conn.state != "ESTAB":
                continue
            # Exclude loopback
            if conn.remote_ip in ("127.0.0.1", "::1", "localhost"):
                continue
                
            key = (conn.remote_ip, conn.remote_port)
            current_keys.add(key)
            
            if key not in self.sessions:
                self.sessions[key] = {
                    "first_seen": now,
                    "last_seen": now,
                    "samples": [(now, conn.bytes_received)],
                }
            else:
                sess = self.sessions[key]
                sess["last_seen"] = now
                sess["samples"].append((now, conn.bytes_received))
                # Keep samples within last 10 minutes
                sess["samples"] = [s for s in sess["samples"] if s[0] >= now - 600.0]
                
        # Remove sessions that have closed
        for key in list(self.sessions.keys()):
            if key not in current_keys:
                del self.sessions[key]
                
        active_count = 0
        idle_count = 0
        
        for key, sess in self.sessions.items():
            first_seen = sess["first_seen"]
            duration = now - first_seen
            
            # If session is newer than the idle threshold, consider it active
            if duration < self.idle_threshold_seconds:
                active_count += 1
                continue
                
            # For sessions older than idle threshold, examine traffic rate over the window
            samples = sess["samples"]
            window_start = now - self.idle_threshold_seconds
            relevant_samples = [s for s in samples if s[0] >= window_start]
            if len(relevant_samples) >= 2:
                time_span = relevant_samples[-1][0] - relevant_samples[0][0]
                bytes_delta = relevant_samples[-1][1] - relevant_samples[0][1]
                rate = (bytes_delta / time_span) if time_span > 0 else 0.0
                if rate > self.max_heartbeat_rate_bps:
                    active_count += 1
                else:
                    idle_count += 1
            else:
                total_bytes = samples[-1][1] - samples[0][1] if len(samples) >= 2 else 0
                rate = (total_bytes / duration) if duration > 0 else 0.0
                if rate > self.max_heartbeat_rate_bps:
                    active_count += 1
                else:
                    idle_count += 1
                    
        return active_count, idle_count

class DownloadTracker:
    """Tracks outbound connections to public IPs for package downloads."""
    def __init__(self, settle_seconds: float = 60.0):
        self.settle_seconds = settle_seconds
        self.last_download_time: Optional[float] = None
        self.prev_outbound_bytes: Dict[Tuple[str, int], int] = {}

    def update(self, outbound_conns: List[ConnectionInfo], now: float) -> Tuple[bool, bool]:
        """
        Update tracker with current outbound connections to public IPs.
        Returns: (is_downloading, is_settling)
        """
        active_downloading = False
        current_keys = set()
        
        for conn in outbound_conns:
            if conn.local_port == 30000:
                continue  # Inbound
            if not is_public_ip(conn.remote_ip):
                continue  # In-cluster or private
            if conn.remote_port not in (80, 443):
                continue
                
            key = (conn.remote_ip, conn.remote_port)
            current_keys.add(key)
            prev = self.prev_outbound_bytes.get(key)
            self.prev_outbound_bytes[key] = conn.bytes_received
            
            # If ESTAB and bytes are flowing, download is active
            if conn.state == "ESTAB":
                if prev is None or conn.bytes_received > prev or conn.bytes_received > 50000:
                    active_downloading = True
                    self.last_download_time = now
                    
        # Clean up closed connections
        for key in list(self.prev_outbound_bytes.keys()):
            if key not in current_keys:
                del self.prev_outbound_bytes[key]
                
        is_settling = False
        if not active_downloading and self.last_download_time is not None:
            if now - self.last_download_time < self.settle_seconds:
                is_settling = True
            else:
                self.last_download_time = None
                
        return active_downloading, is_settling

@dataclass
class ActivityEvaluation:
    is_safe: bool
    reasons: List[str] = field(default_factory=list)
    active_sessions: int = 0
    idle_sessions: int = 0
    downloads_active: bool = False
    downloads_settling: bool = False
    connected_players: int = 0
    world_active: bool = False

def evaluate_activity(
    connected_players: int,
    world_active: bool,
    active_sessions: int,
    idle_sessions: int,
    downloads_active: bool,
    downloads_settling: bool,
) -> ActivityEvaluation:
    reasons: List[str] = []
    
    # 1. Player check: any player in world always blocks (even if idle)
    if connected_players < 0:
        reasons.append("unable to verify connected players")
    elif connected_players > 0:
        reasons.append(f"{connected_players} player(s) connected to world")
        
    # 2. Package downloads always block
    if downloads_active:
        reasons.append("package download in progress")
    elif downloads_settling:
        reasons.append("package download recently completed (settling)")
        
    # 3. Active sessions on Setup / Web
    if active_sessions > 0:
        context = "world" if world_active else "Setup"
        reasons.append(f"{active_sessions} active browser session(s) ({context})")
        
    is_safe = (len(reasons) == 0)
    
    return ActivityEvaluation(
        is_safe=is_safe,
        reasons=reasons,
        active_sessions=active_sessions,
        idle_sessions=idle_sessions,
        downloads_active=downloads_active,
        downloads_settling=downloads_settling,
        connected_players=connected_players,
        world_active=world_active,
    )
