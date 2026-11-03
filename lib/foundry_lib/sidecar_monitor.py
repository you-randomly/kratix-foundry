import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from kubernetes import client, config
from foundry_lib.activity import (
    ActivityEvaluation,
    DownloadTracker,
    SessionTracker,
    evaluate_activity,
    parse_ss,
)
from foundry_lib.foundry_api import check_players
from foundry_lib.presence import (
    DEFAULT_ACK_SNOOZE_SECONDS,
    DEFAULT_CHALLENGE_TIMEOUT_SECONDS,
    PresenceCoordinator,
)

ANNOTATION_REQUESTED_INSTANCE = "foundry.platform/requested-instance"
ANNOTATION_SWITCH_MODE = "foundry.platform/switch-mode"
ANNOTATION_SWITCH_BLOCKED_REASON = "foundry.platform/switch-blocked-reason"
ANNOTATION_SCHEDULED_DELETE = "foundry.platform/scheduled-delete-at"
ANNOTATION_INSTANCE_ACTIVITY = "foundry.platform/activity"

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


def run_ss_probe() -> Optional[str]:
    """Run ss command to inspect socket information. Returns None on failure."""
    try:
        res = subprocess.run(
            ["ss", "-tinH", "state", "established"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout
        print(f"WARN: ss probe exited with {res.returncode}: {res.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"WARN: ss probe failed: {e}", flush=True)
    return None


def inspect_activity(
    session_tracker: SessionTracker,
    download_tracker: DownloadTracker,
    now_ts: float,
    stats: Dict[str, Any],
    ss_output: Optional[str] = None,
) -> ActivityEvaluation:
    """
    Evaluate activity using session and download trackers against socket telemetry.
    If ss_output is None and cannot be probed, fails safe (treats as busy).
    """
    if ss_output is None:
        ss_output = run_ss_probe()

    if ss_output is None:
        # Cannot inspect sockets - fail safe
        return ActivityEvaluation(
            is_safe=False,
            reasons=["network socket inspection unavailable"],
            connected_players=stats.get("connectedPlayers", -1),
            world_active=stats.get("worldActive", False),
        )

    conns = parse_ss(ss_output)
    active_sessions, idle_sessions = session_tracker.update(conns, now_ts)
    downloads_active, downloads_settling = download_tracker.update(conns, now_ts)

    return evaluate_activity(
        connected_players=stats.get("connectedPlayers", 0),
        world_active=stats.get("worldActive", False),
        active_sessions=active_sessions,
        idle_sessions=idle_sessions,
        downloads_active=downloads_active,
        downloads_settling=downloads_settling,
    )


def reconcile_lease(
    coordination_api: Any,
    instance_name: str,
    license_name: str,
    namespace: str,
    stats: Dict[str, Any],
    activity_eval: Optional[ActivityEvaluation] = None,
    is_scheduled_for_delete: bool = False,
    now: Optional[datetime] = None,
    presence_coordinator: Optional[PresenceCoordinator] = None,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Coordinate the Kubernetes Lease for a FoundryLicense from the sidecar container.

    - Acquires the Lease when `foundry.platform/requested-instance == instance_name`
      and the Lease is currently unheld or expired.
    - Renews the Lease while `instance_name` holds it and remains the requested instance.
    - Releases the Lease when another instance (or standby) is requested, respecting
      `foundry.platform/switch-mode` (`block` waits for safe interrupt condition or
      unacknowledged 90s presence challenge; `force` releases immediately).
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
            if presence_coordinator and presence_coordinator.contention_kind in ("switch", "standby"):
                presence_coordinator.clear_contention()
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
        if is_scheduled_for_delete or switch_mode != "block":
            should_block = False
            reasons_text = ""
            if presence_coordinator and presence_coordinator.contention_kind in ("switch", "standby"):
                presence_coordinator.clear_contention()
        elif presence_coordinator is not None and activity_eval is not None:
            contention_kind = "switch" if requested_instance else "standby"
            decision = presence_coordinator.evaluate_contention(
                has_contention=True,
                contention_kind=contention_kind,
                target_instance=requested_instance,
                activity_eval=activity_eval,
                now=now_ts,
            )
            should_block = not decision.should_interrupt
            reasons_text = ", ".join(decision.reasons) if decision.reasons else "activity detected"
        elif activity_eval is not None:
            should_block = not activity_eval.is_safe
            reasons_text = ", ".join(activity_eval.reasons) if activity_eval.reasons else "activity detected"
        else:
            connected_players = stats.get("connectedPlayers", 0)
            should_block = connected_players != 0
            if connected_players > 0:
                reasons_text = f"{connected_players} player(s) connected to '{instance_name}'"
            else:
                reasons_text = f"unable to verify 0 players on '{instance_name}'"

        if should_block:
            target_label = requested_instance or "standby"
            reason = f"Switch to '{target_label}' queued: {reasons_text}"
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

        # Safe to release lease (safe to interrupt, challenge expired, force mode, or scheduled for deletion)
        if presence_coordinator and presence_coordinator.contention_kind in ("switch", "standby"):
            presence_coordinator.mark_released(now_ts)
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

    # Not holding lease: clear any stale switch/standby contention on this instance
    if presence_coordinator and presence_coordinator.contention_kind in ("switch", "standby"):
        presence_coordinator.clear_contention()

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


def _run_monitor_iteration(
    *,
    custom_api: Any,
    coordination_api: Any,
    apps_api: Any,
    namespace: str,
    instance_name: str,
    license_name: Optional[str],
    admin_key_path: str,
    startup_admin_key: str,
    deployment_name: str,
    session_tracker: SessionTracker,
    download_tracker: DownloadTracker,
    http_session: requests.Session,
    presence_coordinator: PresenceCoordinator,
) -> None:
    now_ts = time.time()

    # 1. Read FoundryInstance metadata & spec
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

    # 2. Check Players & World State (Localhost:30000) using persistent session
    current_admin_key = ""
    if os.path.exists(admin_key_path):
        with open(admin_key_path, "r") as f:
            current_admin_key = f.read().strip()
        stats = check_players("localhost:30000", admin_key=current_admin_key, session=http_session)
    else:
        stats = {
            "active": False,
            "connectedPlayers": -1,
            "worldActive": False,
            "worldName": None,
            "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error": "Admin key secret not mounted yet",
        }

    # 3. Evaluate Network & Session Activity
    activity_eval = inspect_activity(
        session_tracker=session_tracker,
        download_tracker=download_tracker,
        now_ts=now_ts,
        stats=stats,
    )
    presence_coordinator.connected_players = activity_eval.connected_players

    # 4. Reconcile License Lease first so releasing a lease happens before any pod restart
    lease_result: Dict[str, Any] = {"isActive": False, "action": "skipped"}
    if resolved_license_name:
        try:
            lease_result = reconcile_lease(
                coordination_api=coordination_api,
                instance_name=instance_name,
                license_name=resolved_license_name,
                namespace=namespace,
                stats=stats,
                activity_eval=activity_eval,
                is_scheduled_for_delete=is_scheduled_for_delete,
                presence_coordinator=presence_coordinator,
                now_ts=now_ts,
            )
            print(
                f"Lease reconcile [{resolved_license_name}]: action={lease_result['action']}, "
                f"holder='{lease_result.get('holder')}', requested='{lease_result.get('requested')}'",
                flush=True,
            )
        except Exception as lease_err:
            print(f"Error reconciling lease '{resolved_license_name}': {lease_err}", flush=True)

    # 5. Check for Password Rotation and Rollout if Safe (or challenge expired)
    password_rotation_pending = bool(
        startup_admin_key and current_admin_key and startup_admin_key != current_admin_key
    )
    pw_reasons: List[str] = []
    if password_rotation_pending:
        if lease_result.get("action") == "blocked_release":
            print(
                f"Admin password rotation also pending for '{instance_name}', "
                "deferring until license switch contention resolves.",
                flush=True,
            )
        else:
            pw_decision = presence_coordinator.evaluate_contention(
                has_contention=True,
                contention_kind="password_reset",
                target_instance=instance_name,
                activity_eval=activity_eval,
                now=now_ts,
            )
            if pw_decision.should_interrupt:
                print(
                    f"Admin password rotation detected on '{instance_name}' and safe to interrupt "
                    f"(state={pw_decision.state}). Triggering rollout restart for '{deployment_name}'...",
                    flush=True,
                )
                restart_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                patch_body = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "foundry.platform/restartedAt": restart_time
                                }
                            }
                        }
                    }
                }
                try:
                    apps_api.patch_namespaced_deployment(
                        name=deployment_name,
                        namespace=namespace,
                        body=patch_body,
                    )
                    presence_coordinator.mark_released(now_ts)
                    print(f"Successfully triggered rollout restart of '{deployment_name}' at {restart_time}.", flush=True)
                    return
                except Exception as deploy_err:
                    print(f"Error triggering deployment restart: {deploy_err}", flush=True)
            else:
                pw_reasons = pw_decision.reasons
                print(
                    f"Admin password rotation pending for '{instance_name}', waiting: {pw_reasons}",
                    flush=True,
                )
    elif lease_result.get("action") != "blocked_release":
        presence_coordinator.clear_contention()

    # 6. Update FoundryInstance Status & Activity Annotation
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

    summary_parts = list(activity_eval.reasons)
    for extra in pw_reasons:
        if extra not in summary_parts:
            summary_parts.append(extra)
    activity_summary = ", ".join(summary_parts) if summary_parts else "idle"
    if inst_annotations.get(ANNOTATION_INSTANCE_ACTIVITY) != activity_summary:
        annot_patch = {
            "metadata": {
                "annotations": {
                    ANNOTATION_INSTANCE_ACTIVITY: activity_summary
                }
            }
        }
        custom_api.patch_namespaced_custom_object(
            group="foundry.platform",
            version="v1alpha1",
            namespace=namespace,
            plural="foundryinstances",
            name=instance_name,
            body=annot_patch,
        )


async def async_monitor_main() -> None:
    namespace = os.getenv("POD_NAMESPACE", "default")
    instance_name = os.getenv("INSTANCE_NAME")
    license_name = os.getenv("LICENSE_NAME")
    admin_key_path = os.getenv("ADMIN_KEY_PATH", "/etc/foundry/credentials/adminPassword")
    startup_admin_key = os.getenv("STARTUP_ADMIN_KEY", "")
    deployment_name = os.getenv("DEPLOYMENT_NAME", f"foundry-{instance_name}")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)))
    idle_threshold = float(os.getenv("SETUP_IDLE_SECONDS", "300"))
    max_heartbeat_bps = float(os.getenv("MAX_HEARTBEAT_BPS", "5.0"))
    settle_seconds = float(os.getenv("DOWNLOAD_SETTLE_SECONDS", "60"))
    proxy_port = int(os.getenv("PROXY_LISTEN_PORT", "8080"))
    upstream_url = os.getenv("FOUNDRY_UPSTREAM_URL", "http://127.0.0.1:30000")
    challenge_timeout = float(
        os.getenv("CHALLENGE_TIMEOUT_SECONDS", str(DEFAULT_CHALLENGE_TIMEOUT_SECONDS))
    )
    ack_snooze = float(
        os.getenv("ACK_SNOOZE_SECONDS", str(DEFAULT_ACK_SNOOZE_SECONDS))
    )
    data_root = os.getenv("FOUNDRY_DATA_ROOT", "/data")
    foundry_version = os.getenv("FOUNDRY_VERSION") or None
    backup_token_ttl = int(os.getenv("BACKUP_TOKEN_TTL_SECONDS", "3600"))

    if not instance_name:
        print("FATAL: INSTANCE_NAME env var not set", flush=True)
        sys.exit(1)

    print(
        f"Starting lease, proxy (:{proxy_port} -> {upstream_url}) & activity monitor for instance='{instance_name}' "
        f"(deployment='{deployment_name}', license='{license_name or 'auto'}') in namespace='{namespace}'...",
        flush=True,
    )

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    custom_api = client.CustomObjectsApi()
    coordination_api = client.CoordinationV1Api()
    apps_api = client.AppsV1Api()

    session_tracker = SessionTracker(
        idle_threshold_seconds=idle_threshold,
        max_heartbeat_rate_bps=max_heartbeat_bps,
    )
    download_tracker = DownloadTracker(settle_seconds=settle_seconds)
    http_session = requests.Session()
    presence_coordinator = PresenceCoordinator(
        instance_name=instance_name,
        challenge_timeout_seconds=challenge_timeout,
        ack_snooze_seconds=ack_snooze,
    )

    loop = asyncio.get_running_loop()
    wake_event = asyncio.Event()

    def trigger_wake() -> None:
        loop.call_soon_threadsafe(wake_event.set)

    def _load_backup_keys() -> List[str]:
        keys: List[str] = []
        if os.path.exists(admin_key_path):
            try:
                with open(admin_key_path, "r", encoding="utf-8") as fh:
                    mounted_key = fh.read().strip()
                if mounted_key:
                    keys.append(mounted_key)
            except OSError:
                pass
        if startup_admin_key and startup_admin_key.strip():
            keys.append(startup_admin_key.strip())
        return keys

    # Start reverse proxy server
    from aiohttp import web
    from foundry_lib.backup import BackupManager
    from foundry_lib.proxy import create_proxy_app

    backup_manager = BackupManager(
        instance_name=instance_name,
        data_root=data_root,
        key_provider=_load_backup_keys,
        foundry_version=foundry_version,
        token_ttl_seconds=backup_token_ttl,
    )

    app = create_proxy_app(
        coordinator=presence_coordinator,
        upstream_url=upstream_url,
        on_state_change=trigger_wake,
        backup_manager=backup_manager,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", proxy_port)
    await site.start()
    print(f"Reverse proxy listening on 0.0.0.0:{proxy_port} -> {upstream_url}", flush=True)

    try:
        while True:
            try:
                await asyncio.to_thread(
                    _run_monitor_iteration,
                    custom_api=custom_api,
                    coordination_api=coordination_api,
                    apps_api=apps_api,
                    namespace=namespace,
                    instance_name=instance_name,
                    license_name=license_name,
                    admin_key_path=admin_key_path,
                    startup_admin_key=startup_admin_key,
                    deployment_name=deployment_name,
                    session_tracker=session_tracker,
                    download_tracker=download_tracker,
                    http_session=http_session,
                    presence_coordinator=presence_coordinator,
                )
            except Exception as e:
                print(f"Error in monitor loop: {str(e)}", flush=True)

            wait_secs = float(poll_interval)
            if (
                presence_coordinator.challenge_deadline is not None
                and presence_coordinator.snoozed_until is None
            ):
                rem = presence_coordinator.challenge_deadline - time.time()
                wait_secs = min(wait_secs, max(0.1, rem + 0.2))

            try:
                await asyncio.wait_for(wake_event.wait(), timeout=wait_secs)
            except asyncio.TimeoutError:
                pass
            wake_event.clear()
    finally:
        await runner.cleanup()


def monitor_loop() -> None:
    asyncio.run(async_monitor_main())


if __name__ == "__main__":
    monitor_loop()
