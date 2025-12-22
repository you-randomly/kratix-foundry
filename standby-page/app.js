// app.js - Standby page logic

const API_BASE = '/api';

// Extract instance name from hostname (e.g., campaign-a.foundry.lab.jake-watson.co.uk)
function getInstanceName() {
  const hostname = window.location.hostname;
  const parts = hostname.split('.');
  return parts[0] || 'unknown';
}

// Initialize page with instance info
async function init() {
  const instanceName = getInstanceName();
  document.getElementById('instanceName').textContent = instanceName;
  
  try {
    const response = await fetch(`${API_BASE}/status?instance=${instanceName}`);
    const data = await response.json();
    
    if (data.activeInstance) {
      document.getElementById('activeName').textContent = data.activeInstance;
      document.getElementById('activeLink').href = 
        `https://${data.activeInstance}.foundry.lab.jake-watson.co.uk`;
    } else {
      document.getElementById('activeSection').innerHTML = 
        '<p style="color: var(--warning)">No active instance found</p>';
    }
  } catch (error) {
    console.error('Failed to fetch status:', error);
    document.getElementById('activeName').textContent = 'Unable to fetch';
  }
}

// Request activation for this instance
async function requestActivation() {
  const instanceName = getInstanceName();
  const btn = document.getElementById('requestBtn');
  const statusEl = document.getElementById('statusMessage');
  
  btn.disabled = true;
  btn.textContent = 'Requesting...';
  statusEl.className = 'status-message info';
  statusEl.textContent = 'Checking for active players...';
  
  try {
    const response = await fetch(`${API_BASE}/activate`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ instance: instanceName }),
    });
    
    const data = await response.json();
    
    if (response.ok) {
      statusEl.className = 'status-message success';
      statusEl.textContent = 'Activation successful! Redirecting...';
      setTimeout(() => {
        window.location.reload();
      }, 2000);
    } else if (response.status === 409) {
      // Blocked due to players
      statusEl.className = 'status-message warning';
      statusEl.textContent = data.message || 
        `Blocked: ${data.connectedPlayers} players connected to ${data.activeInstance}`;
      btn.disabled = false;
      btn.textContent = 'Request Activation';
    } else {
      statusEl.className = 'status-message error';
      statusEl.textContent = data.message || 'Activation failed';
      btn.disabled = false;
      btn.textContent = 'Request Activation';
    }
  } catch (error) {
    console.error('Activation request failed:', error);
    statusEl.className = 'status-message error';
    statusEl.textContent = 'Failed to connect to server';
    btn.disabled = false;
    btn.textContent = 'Request Activation';
  }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', init);
