// server.js - Standby page server with Kubernetes API integration
const http = require('http');
const fs = require('fs');
const path = require('path');

// Kubernetes API configuration (in-cluster)
const K8S_API = 'https://kubernetes.default.svc';
const TOKEN_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/token';
const CA_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt';
const NAMESPACE_PATH = '/var/run/secrets/kubernetes.io/serviceaccount/namespace';

// Default namespace for development (overridden by in-cluster namespace)
const FOUNDRY_NAMESPACE = process.env.FOUNDRY_NAMESPACE || 'foundry-vtt';

// MIME types for static files
const MIME_TYPES = {
    '.html': 'text/html',
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.json': 'application/json',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.ico': 'image/x-icon'
};

// Read Kubernetes service account credentials
function getK8sCredentials() {
    try {
        const token = fs.readFileSync(TOKEN_PATH, 'utf8');
        const ca = fs.readFileSync(CA_PATH);
        const namespace = fs.existsSync(NAMESPACE_PATH)
            ? fs.readFileSync(NAMESPACE_PATH, 'utf8').trim()
            : FOUNDRY_NAMESPACE;
        return { token, ca, namespace };
    } catch (err) {
        console.log('Running outside cluster, using development mode');
        return null;
    }
}

// Make Kubernetes API request
async function k8sRequest(path, credentials) {
    if (!credentials) {
        throw new Error('Not running in Kubernetes cluster');
    }

    const https = require('https');
    const url = `${K8S_API}${path}`;

    return new Promise((resolve, reject) => {
        const options = {
            headers: {
                'Authorization': `Bearer ${credentials.token}`,
                'Accept': 'application/json'
            },
            ca: credentials.ca
        };

        https.get(url, options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try {
                    resolve(JSON.parse(data));
                } catch (e) {
                    reject(new Error(`Failed to parse response: ${data}`));
                }
            });
        }).on('error', reject);
    });
}

// Get instance name from hostname (e.g., campaign-a.foundry.example.com -> campaign-a)
function getInstanceFromHost(host) {
    if (!host) return null;
    const parts = host.split('.');
    return parts[0] || null;
}

// Find license for an instance
async function findLicenseForInstance(instanceName, credentials) {
    const primaryNs = credentials?.namespace || FOUNDRY_NAMESPACE;
    const namespaces = [primaryNs];
    if (primaryNs !== 'default') namespaces.push('default');

    let instance = null;
    let targetNs = null;

    for (const ns of namespaces) {
        const result = await k8sRequest(
            `/apis/foundry.platform/v1alpha1/namespaces/${ns}/foundryinstances/${instanceName}`,
            credentials
        );
        if (result.code !== 404) {
            instance = result;
            targetNs = ns;
            break;
        }
    }

    // If instance still not found (404), return specific status
    if (!instance) {
        return { status: 'deleted', error: 'Instance not found' };
    }

    const licenseName = instance.spec?.licenseRef?.name;
    // Check for scheduled deletion
    const scheduledDeleteAt = instance.metadata?.annotations?.['foundry.platform/scheduled-delete-at'];

    if (!licenseName) {
        return { error: 'Instance has no license reference', scheduledDeleteAt };
    }

    // Get the license (try same namespace as instance first)
    let license = await k8sRequest(
        `/apis/foundry.platform/v1alpha1/namespaces/${targetNs}/foundrylicenses/${licenseName}`,
        credentials
    );

    // Fallback to searching primary namespace for the license if not in same namespace
    if (license.code === 404 && targetNs !== primaryNs) {
        license = await k8sRequest(
            `/apis/foundry.platform/v1alpha1/namespaces/${primaryNs}/foundrylicenses/${licenseName}`,
            credentials
        );
    }

    if (license.code === 404) {
        return { error: 'License not found', scheduledDeleteAt };
    }

    const activeInstanceName = license.spec?.activeInstanceName || null;

    // Fetch the active instance's stats directly for real-time player counts
    let activeInstanceStats = null;
    if (activeInstanceName) {
        try {
            const activeInstance = await k8sRequest(
                `/apis/foundry.platform/v1alpha1/namespaces/${targetNs}/foundryinstances/${activeInstanceName}`,
                credentials
            );
            if (activeInstance && activeInstance.status) {
                activeInstanceStats = {
                    connectedPlayers: activeInstance.status.connectedPlayers ?? null,
                    worldActive: activeInstance.status.worldActive ?? null,
                    activeWorld: activeInstance.status.activeWorld || null,
                    checkedAt: activeInstance.status.lastSidecarUpdate || null
                };
            }
        } catch (err) {
            console.log(`Failed to fetch active instance ${activeInstanceName}: ${err.message}`);
        }
    }

    return {
        instanceName: instanceName,
        licenseName: licenseName,
        activeInstance: activeInstanceName,
        connectedPlayers: activeInstanceStats?.connectedPlayers ?? null,
        worldActive: activeInstanceStats?.worldActive ?? null,
        activeWorld: activeInstanceStats?.activeWorld || null,
        checkedAt: activeInstanceStats?.checkedAt || null,
        scheduledDeleteAt: scheduledDeleteAt || null
    };
}

// Handle API requests
async function handleApi(req, res, credentials) {
    const url = new URL(req.url, `http://${req.headers.host}`);

    if (url.pathname === '/api/status') {
        const instanceName = url.searchParams.get('instance');

        if (!instanceName) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Missing instance parameter' }));
            return;
        }

        try {
            const status = await findLicenseForInstance(instanceName, credentials);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(status));
        } catch (err) {
            console.error('API error:', err.message);
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: err.message }));
        }
        return;
    }

    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
}

// Handle static file requests
function handleStatic(req, res) {
    let filePath = req.url === '/' ? '/index.html' : req.url;
    filePath = path.join(__dirname, filePath.split('?')[0]);

    const ext = path.extname(filePath);
    const contentType = MIME_TYPES[ext] || 'application/octet-stream';

    fs.readFile(filePath, (err, content) => {
        if (err) {
            if (err.code === 'ENOENT') {
                // Serve index.html for any unmatched routes (SPA behavior)
                fs.readFile(path.join(__dirname, 'index.html'), (err2, indexContent) => {
                    if (err2) {
                        res.writeHead(404);
                        res.end('Not found');
                    } else {
                        res.writeHead(200, { 'Content-Type': 'text/html' });
                        res.end(indexContent);
                    }
                });
            } else {
                res.writeHead(500);
                res.end('Server error');
            }
        } else {
            res.writeHead(200, { 'Content-Type': contentType });
            res.end(content);
        }
    });
}

// Main server
const credentials = getK8sCredentials();
const PORT = process.env.PORT || 8080;

const server = http.createServer(async (req, res) => {
    // Enable CORS for development
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

    if (req.method === 'OPTIONS') {
        res.writeHead(204);
        res.end();
        return;
    }

    // Route API requests
    if (req.url.startsWith('/api/')) {
        await handleApi(req, res, credentials);
        return;
    }

    // Serve static files
    handleStatic(req, res);
});

server.listen(PORT, () => {
    console.log(`Standby page server running on port ${PORT}`);
    console.log(`Kubernetes API: ${credentials ? 'connected' : 'development mode'}`);
    console.log(`Namespace: ${credentials?.namespace || FOUNDRY_NAMESPACE}`);
});
