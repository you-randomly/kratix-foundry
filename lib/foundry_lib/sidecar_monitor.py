import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from kubernetes import client, config
from foundry_lib.foundry_api import check_players

ANNOTATION_REQUESTED_INSTANCE = "foundry.platform/requested-instance"
ANNOTATION_SWITCH_MODE = "foundry.platform/switch-mode"
ANNOTATION_SWITCH_BLOCKED_REASON = "foundry.platform/switch-blocked-reason"
ANNOTATION_SCHEDULED_DELETE = "foundry.platform/scheduled-delete-at"

DEFAULT_LEASE_DURATION_SECONDS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 15


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime object is timezone-aware in UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_lease_expired(lease: Any, now: Optional[datetime] = None) -> bool:
    """Return True if the Lease has no holder or its TTL has elapsed."""
    now = _ensure_utc(now) or datetime.now(timezone.utc)
    spec = getattr(lease, "spec", None)
    if spec is None:
        return True

    holder = (getattr(spec, "holder_identity", None) or "").strip()
    if not holder:
        return True

    duration = getattr(spec, "lease_duration_seconds", None) or DEFAULT_LEASE_DURATION_SECONDS
    renew_time = _ensure_utc(getattr(spec, "renew_time", None) or getattr(spec, "acquire_time", None))
    if renew_time is None:
        return True

    return now > (renew_time + timedelta(seconds=duration))


def reconcile_lease(
    coordination_api: Any,
    instance_name: str,
    license_name: str,
    namespace: str,
    stats: Dict[str, Any],
    is_scheduled_for_delete: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Coordinate the Kubernetes Lease for a FoundryLicense from the sidecar container.

    - Acquires the Lease when `foundry.platform/requested-instance == instance_name`
      and the Lease is currently unheld or expired.
    - Renews the Lease while `instance_name` holds it and remains the requested instance.
    - Releases the Lease when another instance (or standby) is requested, respecting
      `foundry.platform/switch-mode` (`block` waits for 0 connectedPlayers; `force` releases immediately).
    """
    now = _ensure_utc(now) or datetime.now(timezone.utc)
    lease = coordination_api.read_namespaced_lease(name=license_name, namespace=namespace)

    if lease.metadata.annotations is None:
        lease.metadata.annotations = {}
    annotations = lease.metadata.annotations

    requested_instance = (annotations.get(ANNOTATION_REQUESTED_INSTANCE) or "").strip()
    switch_mode = (annotations.get(ANNOTATION_SWITCH_MODE) or "block").strip().lower()

    if lease.spec is None:
        lease.spec = client.V1LeaseSpec(lease_duration_seconds=DEFAULT_LEASE_DURATION_SECONDS)

    current_holder = (lease.spec.holder_identity or "").strip()
    expired = is_lease_expired(lease, now=now)

    # Case 1: This instance currently holds the lease (and it hasn't expired)
    if current_holder == instance_name and not expired:
        if requested_instance == instance_name and not is_scheduled_for_delete:
            # Renew lease
            lease.spec.renew_time = now
            annotations.pop(ANNOTATION_SWITCH_BLOCKED_REASON, None)
            coordination_api.replace_namespaced_lease(
                name=license_name, namespace=namespace, body=lease
            )
            return {
                "action": "renewed",
                "isActive": True,
                "holder": instance_name,
                "requested": requested_instance,
            }

        # Another instance (or standby) was requested, or this instance is scheduled for deletion
        connected_players = stats.get("connectedPlayers", 0)
        should_block = (
            not is_scheduled_for_delete
            and switch_mode == "block"
            and connected_players != 0
        )

        if should_block:
            target_label = requested_instance or "standby"
            if connected_players > 0:
                reason = (
                    f"Switch to '{target_label}' queued: "
                    f"{connected_players} player(s) connected to '{instance_name}'"
                )
            else:
                reason = (
                    f"Switch to '{target_label}' queued: "
                    f"unable to verify 0 players on '{instance_name}'"
                )
            lease.spec.renew_time = now
            annotations[ANNOTATION_SWITCH_BLOCKED_REASON] = reason
            coordination_api.replace_namespaced_lease(
                name=license_name, namespace=namespace, body=lease
            )
            return {
                "action": "blocked_release",
                "isActive": True,
                "holder": instance_name,
                "requested": requested_instance,
                "blockedReason": reason,
            }

        # Safe to release lease (0 players, force mode, or scheduled for deletion)
        lease.spec.holder_identity = ""
        lease.spec.renew_time = None
        annotations.pop(ANNOTATION_SWITCH_BLOCKED_REASON, None)
        coordination_api.replace_namespaced_lease(
            name=license_name, namespace=namespace, body=lease
        )
        return {
            "action": "released",
            "isActive": False,
            "holder": "",
            "requested": requested_instance,
        }

    # Case 2: This instance is requested to become active and does not currently hold a valid lease
    if requested_instance == instance_name and not is_scheduled_for_delete:
        if not current_holder or expired:
            lease.spec.holder_identity = instance_name
            lease.spec.acquire_time = now
            lease.spec.renew_time = now
            lease.spec.lease_transitions = (lease.spec.lease_transitions or 0) + 1
            if not lease.spec.lease_duration_seconds:
                lease.spec.lease_duration_seconds = DEFAULT_LEASE_DURATION_SECONDS
            annotations.pop(ANNOTATION_SWITCH_BLOCKED_REASON, None)
            coordination_api.replace_namespaced_lease(
                name=license_name, namespace=namespace, body=lease
            )
            return {
                "action": "acquired",
                "isActive": True,
                "holder": instance_name,
                "requested": requested_instance,
            }

        return {
            "action": "waiting",
            "isActive": False,
            "holder": current_holder,
            "requested": requested_instance,
        }

    # Case 3: Standby
    return {
        "action": "standby",
        "isActive": False,
        "holder": current_holder if not expired else "",
        "requested": requested_instance,
    }


def monitor_loop():
    namespace = os.getenv("POD_NAMESPACE", "default")
    instance_name = os.getenv("INSTANCE_NAME")
    license_name = os.getenv("LICENSE_NAME")
    admin_key_path = os.getenv("ADMIN_KEY_PATH", "/etc/foundry/credentials/adminPassword")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)))

    if not instance_name:
        print("FATAL: INSTANCE_NAME env var not set", flush=True)
        sys.exit(1)

    print(
        f"Starting lease & status monitor for instance='{instance_name}' "
        f"(license='{license_name or 'auto'}') in namespace='{namespace}'...",
        flush=True,
    )

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    coordination_api = client.CoordinationV1Api()

    while True:
        try:
            # 1. Read FoundryInstance metadata & spec (for licenseRef and scheduled-delete check)
            instance_obj = custom_api.get_namespaced_custom_object(
                group="foundry.platform",
                version="v1alpha1",
                namespace=namespace,
                plural="foundryinstances",
                name=instance_name,
            )
            inst_annotations = instance_obj.get("metadata", {}).get("annotations", {}) or {}
            is_scheduled_for_delete = bool(inst_annotations.get(ANNOTATION_SCHEDULED_DELETE))
            resolved_license_name = license_name or (
                instance_obj.get("spec", {}).get("licenseRef", {}).get("name")
            )

            # 2. Check Players (Localhost:30000)
            if os.path.exists(admin_key_path):
                with open(admin_key_path, "r") as f:
                    admin_key = f.read().strip()
                stats = check_players("localhost:30000", admin_key)
            else:
                print(f"Waiting for admin key at {admin_key_path}...", flush=True)
                stats = {
                    "connectedPlayers": -1,
                    "worldActive": False,
                    "worldName": None,
                    "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "error": "Admin key secret not mounted yet",
                }

            # 3. Reconcile License Lease
            lease_result = {"isActive": False, "action": "skipped"}
            if resolved_license_name:
                try:
                    lease_result = reconcile_lease(
                        coordination_api=coordination_api,
                        instance_name=instance_name,
                        license_name=resolved_license_name,
                        namespace=namespace,
                        stats=stats,
                        is_scheduled_for_delete=is_scheduled_for_delete,
                    )
                    print(
                        f"Lease reconcile [{resolved_license_name}]: action={lease_result['action']}, "
                        f"holder='{lease_result.get('holder')}', requested='{lease_result.get('requested')}'",
                        flush=True,
                    )
                except Exception as lease_err:
                    print(f"Error reconciling lease '{resolved_license_name}': {lease_err}", flush=True)

            # 4. Update FoundryInstance Status
            body = {
                "status": {
                    "isActive": lease_result.get("isActive", False),
                    "connectedPlayers": stats.get("connectedPlayers", 0),
                    "activeWorld": stats.get("worldName"),
                    "worldActive": stats.get("worldActive", False),
                    "lastSidecarUpdate": stats.get("checkedAt"),
                    "error": stats.get("error"),
                }
            }

            custom_api.patch_namespaced_custom_object_status(
                group="foundry.platform",
                version="v1alpha1",
                namespace=namespace,
                plural="foundryinstances",
                name=instance_name,
                body=body,
            )

        except Exception as e:
            print(f"Error in monitor loop: {str(e)}", flush=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    monitor_loop()
