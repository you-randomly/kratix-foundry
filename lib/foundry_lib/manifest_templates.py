def httproute_template(name, namespace, hostname, gateway_name, gateway_ns, backend_service, backend_ns=None):
    rule = {
        "backendRefs": [
            {
                "name": backend_service,
                "port": 80
            }
        ]
    }
    if backend_ns:
        rule["backendRefs"][0]["namespace"] = backend_ns

    return {
        "apiVersion": "gateway.networking.k8s.io/v1beta1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": f"foundry-id-{name}",
            "namespace": namespace,
            "labels": {
                "app": "foundry-vtt",
                "instance": name
            }
        },
        "spec": {
            "hostnames": [hostname],
            "parentRefs": [
                {
                    "name": gateway_name,
                    "namespace": gateway_ns
                }
            ],
            "rules": [rule]
        }
    }

def dnsendpoint_template(name, namespace, hostname, dns_target):
    return {
        "apiVersion": "externaldns.k8s.io/v1alpha1",
        "kind": "DNSEndpoint",
        "metadata": {
            "name": f"foundry-id-{name}",
            "namespace": namespace
        },
        "spec": {
            "endpoints": [
                {
                    "dnsName": hostname,
                    "recordTTL": 300,
                    "recordType": "A",
                    "targets": [dns_target]
                }
            ]
        }
    }

def credentials_secret_template(name, namespace, admin_password):
    import base64
    encoded_pw = base64.b64encode(admin_password.encode()).decode()
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace
        },
        "type": "Opaque",
        "data": {
            "adminPassword": encoded_pw
        }
    }

def deployment_template(name, namespace, version, cpu, memory, hostname, proxy_ssl, proxy_port, volume_def, admin_secret_name, monitor_image=None, storage_backend="pvc"):
    # Only use chown init container for PVC storage, NFS doesn't allow ownership changes
    init_containers = []
    if storage_backend == "pvc":
        init_containers = [
            {
                "name": "volume-permissions",
                "image": "busybox:1.36",
                "command": ["sh", "-c", "chown -R 1000:1000 /data"],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}]
            }
        ]
    
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"foundry-{name}",
            "namespace": namespace,
            "labels": {
                "app": "foundry-vtt",
                "instance": name
            }
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {
                "matchLabels": {
                    "app": "foundry-vtt",
                    "instance": name
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": "foundry-vtt",
                        "instance": name
                    }
                },
                "spec": {
                    "serviceAccountName": f"foundry-{name}-monitor",
                    "initContainers": init_containers,
                    "containers": [
                        {
                            "name": "foundry-vtt",
                            "image": f"felddy/foundryvtt:{version}",
                            "ports": [{"containerPort": 30000}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "runAsNonRoot": False,
                                "seccompProfile": {"type": "RuntimeDefault"}
                            },
                            "resources": {
                                "requests": {
                                    "cpu": cpu,
                                    "memory": memory
                                }
                            },
                            "env": [
                                {"name": "UV_THREADPOOL_SIZE", "value": "6"},
                                {"name": "CONTAINER_CACHE", "value": "/data/container_cache"},
                                {"name": "TIMEZONE", "value": "UTC"},
                                {"name": "FOUNDRY_HOSTNAME", "value": hostname},
                                {"name": "FOUNDRY_LOCAL_HOSTNAME", "value": hostname},
                                {"name": "FOUNDRY_PROXY_SSL", "value": str(proxy_ssl).lower()},
                                {"name": "FOUNDRY_PROXY_PORT", "value": str(proxy_port)},
                                {
                                    "name": "FOUNDRY_USERNAME",
                                    "valueFrom": {"secretKeyRef": {"name": "foundry-credentials", "key": "username"}}
                                },
                                {
                                    "name": "FOUNDRY_PASSWORD",
                                    "valueFrom": {"secretKeyRef": {"name": "foundry-credentials", "key": "password"}}
                                },
                                {
                                    "name": "FOUNDRY_ADMIN_KEY",
                                    "valueFrom": {"secretKeyRef": {"name": admin_secret_name, "key": "adminPassword"}}
                                },
                                {
                                    "name": "FOUNDRY_LICENSE_KEY",
                                    "valueFrom": {"secretKeyRef": {"name": "foundry-license", "key": "license-key"}}
                                }
                            ],
                            "volumeMounts": [{"name": "data", "mountPath": "/data"}]
                        }
                    ],
                    "volumes": [{"name": "data", **volume_def}]
                }
            }
        }
    }

    if monitor_image:
        deployment["spec"]["template"]["spec"]["containers"].append({
            "name": "monitor",
            "image": monitor_image,
            "command": ["python3", "-m", "foundry_lib.sidecar_monitor"],
            "env": [
                {"name": "INSTANCE_NAME", "value": name},
                {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
                {"name": "PYTHONPATH", "value": "/app"},
                {"name": "PYTHONUNBUFFERED", "value": "1"}
            ],
            "volumeMounts": [
                {
                    "name": "credentials",
                    "mountPath": "/etc/foundry/credentials",
                    "readOnly": True
                }
            ]
        })
        # Add credentials volume to top level
        deployment["spec"]["template"]["spec"]["volumes"].append({
            "name": "credentials",
            "secret": {"secretName": "foundry-credentials"}
        })

    return deployment

def rbac_templates(name, namespace):
    return [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": f"foundry-{name}-monitor",
                "namespace": namespace
            }
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {
                "name": f"foundry-{name}-monitor",
                "namespace": namespace
            },
            "rules": [
                {
                    "apiGroups": ["foundry.platform"],
                    "resources": ["foundryinstances/status"],
                    "verbs": ["get", "patch", "update"]
                }
            ]
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": f"foundry-{name}-monitor",
                "namespace": namespace
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": f"foundry-{name}-monitor",
                    "namespace": namespace
                }
            ],
            "roleRef": {
                "kind": "Role",
                "name": f"foundry-{name}-monitor",
                "apiGroup": "rbac.authorization.k8s.io"
            }
        }
    ]

def service_template(name, namespace):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": f"foundry-{name}",
            "namespace": namespace,
            "labels": {
                "app": "foundry-vtt",
                "instance": name
            }
        },
        "spec": {
            "selector": {
                "app": "foundry-vtt",
                "instance": name
            },
            "ports": [{"protocol": "TCP", "port": 80, "targetPort": 30000}],
            "type": "ClusterIP"
        }
    }

def pvc_template(name, namespace, storage="10Gi"):
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": f"foundry-{name}-data",
            "namespace": namespace,
            "labels": {
                "app": "foundry-vtt",
                "instance": name
            }
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": storage}}
        }
    }
