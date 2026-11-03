"""
Tracks Discord status messages for /vtt-update switches and keeps embeds synchronized
across 90s presence challenges, 15m snoozes, superseding commands, and subsequent switches.
"""

import asyncio
import json
import time
from typing import Any, Dict, Optional

import discord

from embeds import format_instance_embed
import k8s_client as k8s

ANNOTATION_DISCORD_TRACKED_MESSAGES = "foundry.platform/discord-tracked-messages"
WEBHOOK_SAFE_TTL_SECONDS = 840.0       # 14m (safely inside Discord's 15m interaction token window)
PROGRESS_EDIT_INTERVAL_SECONDS = 15.0  # Throttle countdown refreshes to avoid rate limits
MAX_PENDING_AGE_SECONDS = 4 * 3600.0   # 4h safety ceiling for orphaned pending records


class SwitchTracker:
    """Coordinates live and background Discord message updates for license transitions."""

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._webhooks: Dict[int, Dict[str, Any]] = {}

    def _key(self, namespace: str, license_name: str) -> str:
        return f"{namespace}/{license_name}"

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _ensure_loaded_from_license(self, lic: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
        meta = lic.get("metadata", {}) or {}
        ns = meta.get("namespace") or k8s.FOUNDRY_NAMESPACE
        name = meta.get("name", "")
        key = self._key(ns, name)
        if key in self._state:
            return self._state[key]

        raw = (meta.get("annotations") or {}).get(ANNOTATION_DISCORD_TRACKED_MESSAGES)
        parsed: Dict[str, Optional[Dict[str, Any]]] = {"active_msg": None, "pending_msg": None}
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    parsed["active_msg"] = loaded.get("active_msg")
                    parsed["pending_msg"] = loaded.get("pending_msg")
            except Exception as e:
                print(f"Failed to parse tracked Discord messages on {key}: {e}")

        self._state[key] = parsed
        return parsed

    async def _persist_state(
        self,
        license_name: str,
        namespace: str,
        state: Dict[str, Optional[Dict[str, Any]]],
    ) -> None:
        clean_active = None
        if state.get("active_msg"):
            am = state["active_msg"]
            clean_active = {
                "channel_id": am.get("channel_id"),
                "message_id": am.get("message_id"),
                "instance": am.get("instance"),
                "user_id": am.get("user_id"),
                "namespace": am.get("namespace") or namespace,
            }

        clean_pending = None
        if state.get("pending_msg"):
            pm = state["pending_msg"]
            clean_pending = {
                "action": pm.get("action"),
                "instance": pm.get("instance"),
                "channel_id": pm.get("channel_id"),
                "message_id": pm.get("message_id"),
                "user_id": pm.get("user_id"),
                "namespace": pm.get("namespace") or namespace,
                "created_at": pm.get("created_at"),
                "was_queued": bool(pm.get("was_queued")),
                "stage": pm.get("stage") or "",
            }

        if clean_active is None and clean_pending is None:
            annot_val = None
        else:
            annot_val = json.dumps({"active_msg": clean_active, "pending_msg": clean_pending})

        try:
            await asyncio.to_thread(
                k8s.annotate_foundry_license,
                license_name,
                {ANNOTATION_DISCORD_TRACKED_MESSAGES: annot_val},
                namespace,
            )
        except Exception as e:
            print(f"Failed to persist Discord message state on license {license_name}: {e}")

    async def _edit_message(
        self,
        bot: Any,
        msg_info: Dict[str, Any],
        embed: discord.Embed,
        now: float,
    ) -> str:
        """Edit a tracked Discord message via live WebhookMessage (<14m) or permanent Bot PartialMessage."""
        mid_raw = msg_info.get("message_id")
        if not mid_raw:
            return "not_found"
        mid = int(mid_raw)

        wh = self._webhooks.get(mid)
        if wh is not None and (now - wh["created_at"]) < WEBHOOK_SAFE_TTL_SECONDS:
            try:
                await wh["webhook_msg"].edit(embed=embed)
                return "ok"
            except discord.NotFound as nf:
                # 10008 = Unknown Message (deleted); 10015 = Unknown Webhook (expired)
                if getattr(nf, "code", None) == 10008:
                    self._webhooks.pop(mid, None)
                    return "not_found"
                self._webhooks.pop(mid, None)
            except Exception as e:
                print(f"Webhook edit failed for message {mid}, falling back to channel edit: {e}")
                self._webhooks.pop(mid, None)

        cid_raw = msg_info.get("channel_id")
        if not cid_raw:
            return "failed"
        cid = int(cid_raw)

        try:
            channel = bot.get_channel(cid)
            if channel is None:
                channel = await bot.fetch_channel(cid)
            if channel is None or not hasattr(channel, "get_partial_message"):
                return "failed"
            partial = channel.get_partial_message(mid)
            await partial.edit(embed=embed)
            return "ok"
        except discord.NotFound:
            return "not_found"
        except Exception as e:
            print(f"Channel partial edit failed for message {mid} in channel {cid}: {e}")
            return "failed"

    async def _send_completion_ping(
        self,
        bot: Any,
        msg_info: Dict[str, Any],
        text: str,
        now: float,
    ) -> None:
        """Send a brief notification ping when a queued/delayed switch completes."""
        mid_raw = msg_info.get("message_id")
        cid_raw = msg_info.get("channel_id")
        uid_raw = msg_info.get("user_id")

        # 1. Try replying in the channel via Bot token first (clean reply link to the updated embed)
        if cid_raw and mid_raw:
            try:
                cid = int(cid_raw)
                mid = int(mid_raw)
                channel = bot.get_channel(cid)
                if channel is None:
                    channel = await bot.fetch_channel(cid)
                if channel is not None and hasattr(channel, "send"):
                    ref_cls = getattr(discord, "MessageReference", None)
                    if callable(ref_cls):
                        ref = ref_cls(message_id=mid, channel_id=cid, fail_if_not_exists=False)
                        await channel.send(content=text, reference=ref)
                    else:
                        await channel.send(content=text)
                    return
            except Exception as e:
                print(f"Channel reply ping failed for message {mid_raw}, trying fallback: {e}")

        # 2. Try interaction followup if still within the 14m webhook token window
        if mid_raw:
            wh = self._webhooks.get(int(mid_raw))
            if wh is not None and (now - wh["created_at"]) < WEBHOOK_SAFE_TTL_SECONDS:
                try:
                    await wh["interaction"].followup.send(content=text)
                    return
                except Exception as e:
                    print(f"Interaction followup ping failed for message {mid_raw}: {e}")

        # 3. Final fallback: DM the requester directly
        if uid_raw:
            try:
                user = await bot.fetch_user(int(uid_raw))
                if user is not None:
                    await user.send(content=text)
            except Exception as e:
                print(f"DM fallback ping failed for user {uid_raw}: {e}")

    @staticmethod
    def _classify_stage(blocked_reason: str, elapsed: float) -> str:
        low = blocked_reason.lower()
        if "awaiting user confirmation" in low:
            return "challenging"
        if "active session confirmed" in low:
            return "snoozed"
        if blocked_reason:
            return "blocked"
        if elapsed >= 20.0:
            return "completing"
        return "switching"

    @staticmethod
    def _build_queued_embed(
        action: str,
        target_instance: str,
        license_name: str,
        active_holder: str,
        blocked_reason: str,
        stage: str,
    ) -> discord.Embed:
        holder_label = active_holder or "the active instance"
        if action == "activate":
            if stage == "challenging":
                embed = discord.Embed(
                    title="⏳ Waiting for Active User Confirmation",
                    description=(
                        f"Sent an **\"Are you still using Foundry?\"** prompt to **{holder_label}**.\n"
                        f"If nobody clicks *\"I'm still here\"*, **{target_instance}** will go live automatically.\n\n"
                        f"**Status:** {blocked_reason}"
                    ),
                    color=discord.Color.gold(),
                )
            elif stage == "snoozed":
                embed = discord.Embed(
                    title="⏳ Switch Queued — Active Session Confirmed",
                    description=(
                        f"A user on **{holder_label}** confirmed they are still actively using Foundry.\n"
                        f"**{target_instance}** remains queued on **{license_name}** and will activate automatically "
                        f"once their 15-minute window ends or they close Foundry.\n\n"
                        f"**Status:** {blocked_reason}"
                    ),
                    color=discord.Color.gold(),
                )
            elif stage == "blocked":
                embed = discord.Embed(
                    title="⏳ Switch Queued (Waiting for Safe State)",
                    description=(
                        f"**{target_instance}** is queued to acquire license **{license_name}** as soon as "
                        f"**{holder_label}** is safe to interrupt.\n\n"
                        f"**Reason:** {blocked_reason}"
                    ),
                    color=discord.Color.gold(),
                )
            else:
                embed = discord.Embed(
                    title=f"🔄 Completing Switch to {target_instance}...",
                    description=(
                        f"Handing over license **{license_name}** to **{target_instance}**..."
                    ),
                    color=discord.Color.blue(),
                )
            embed.set_footer(text="This message updates automatically and will ping you once live.")
            return embed

        # Deactivate
        if stage == "challenging":
            embed = discord.Embed(
                title="⏳ Waiting for Active User Confirmation",
                description=(
                    f"Sent an **\"Are you still using Foundry?\"** prompt to **{target_instance}**.\n"
                    f"If nobody clicks *\"I'm still here\"*, **{target_instance}** will move to standby automatically.\n\n"
                    f"**Status:** {blocked_reason}"
                ),
                color=discord.Color.gold(),
            )
        elif stage == "snoozed":
            embed = discord.Embed(
                title="⏳ Standby Queued — Active Session Confirmed",
                description=(
                    f"A user on **{target_instance}** confirmed they are still actively using Foundry.\n"
                    f"**{target_instance}** will move to standby once their 15-minute window ends or they close Foundry.\n\n"
                    f"**Status:** {blocked_reason}"
                ),
                color=discord.Color.gold(),
            )
        elif stage == "blocked":
            embed = discord.Embed(
                title="⏳ Standby Queued (Waiting for Safe State)",
                description=(
                    f"**{target_instance}** will move to standby as soon as it is safe to interrupt.\n\n"
                    f"**Reason:** {blocked_reason}"
                ),
                color=discord.Color.gold(),
            )
        else:
            embed = discord.Embed(
                title=f"🟠 Completing Standby for {target_instance}...",
                description=f"Releasing license **{license_name}** from **{target_instance}**...",
                color=discord.Color.orange(),
            )
        embed.set_footer(text="This message updates automatically once standby completes.")
        return embed

    async def _demote_active_message(
        self,
        bot: Any,
        active_msg: Dict[str, Any],
        license_name: str,
        new_active_instance: str,
        now: float,
    ) -> None:
        old_name = active_msg.get("instance", "")
        old_ns = active_msg.get("namespace") or k8s.FOUNDRY_NAMESPACE
        old_inst = await asyncio.to_thread(k8s.get_foundry_instance, old_name, old_ns)
        if old_inst:
            embed = format_instance_embed(old_inst, is_active_override=False)
            embed.title = f"🟠 Instance Now on Standby: {old_name}"
        else:
            embed = discord.Embed(
                title=f"🟠 Instance Now on Standby: {old_name}",
                color=discord.Color.orange(),
            )

        if new_active_instance:
            embed.description = f"License **{license_name}** moved to **{new_active_instance}**."
        else:
            embed.description = f"License **{license_name}** was released; **{old_name}** is now on standby."

        await self._edit_message(bot, active_msg, embed, now)
        if active_msg.get("message_id"):
            self._webhooks.pop(int(active_msg["message_id"]), None)

    async def register_pending(
        self,
        *,
        bot: Any,
        license_name: str,
        namespace: Optional[str] = None,
        action: str,
        instance_name: str,
        user_id: str,
        channel_id: Optional[int],
        message_id: int,
        webhook_msg: Any = None,
        interaction: Any = None,
        now: Optional[float] = None,
    ) -> None:
        ns = namespace or k8s.FOUNDRY_NAMESPACE
        now_ts = time.time() if now is None else now
        key = self._key(ns, license_name)

        async with self._get_lock(key):
            lic = await asyncio.to_thread(k8s.get_foundry_license, license_name, ns, False)
            if lic:
                state = self._ensure_loaded_from_license(lic)
            else:
                state = self._state.setdefault(key, {"active_msg": None, "pending_msg": None})

            old_pending = state.get("pending_msg")
            if old_pending and int(old_pending.get("message_id", 0)) != int(message_id):
                superseded_embed = discord.Embed(
                    title="🚫 Request Superseded",
                    description=(
                        f"A newer `/vtt-update` command (`{action}` **{instance_name}**) "
                        f"replaced this queued request on **{license_name}**."
                    ),
                    color=discord.Color.light_grey(),
                )
                await self._edit_message(bot, old_pending, superseded_embed, now_ts)
                self._webhooks.pop(int(old_pending.get("message_id", 0)), None)

            if webhook_msg is not None:
                self._webhooks[int(message_id)] = {
                    "webhook_msg": webhook_msg,
                    "interaction": interaction,
                    "created_at": now_ts,
                }

            state["pending_msg"] = {
                "action": action,
                "instance": instance_name,
                "channel_id": channel_id,
                "message_id": int(message_id),
                "user_id": str(user_id),
                "namespace": ns,
                "created_at": now_ts,
                "was_queued": False,
                "stage": "switching",
                "last_reason": "",
                "last_edit_at": 0.0,
            }
            await self._persist_state(license_name, ns, state)

    async def reconcile_license(
        self,
        bot: Any,
        license_name: str,
        namespace: Optional[str] = None,
        lic_obj: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> str:
        """
        Reconcile tracked Discord messages for a single FoundryLicense.
        Returns one of: 'idle', 'waiting', 'queued', 'completed', 'superseded'.
        """
        ns = namespace or k8s.FOUNDRY_NAMESPACE
        now_ts = time.time() if now is None else now
        key = self._key(ns, license_name)

        async with self._get_lock(key):
            lic = lic_obj or await asyncio.to_thread(k8s.get_foundry_license, license_name, ns, False)
            if not lic:
                return "idle"

            state = self._ensure_loaded_from_license(lic)
            active_msg = state.get("active_msg")
            pending = state.get("pending_msg")
            if not active_msg and not pending:
                return "idle"

            active_instance = (lic.get("status", {}).get("activeInstance") or "").strip()
            requested_instance = (lic.get("spec", {}).get("activeInstanceName") or "").strip()
            blocked_reason = (lic.get("status", {}).get("blockedReason") or "").strip()
            base_domain = (
                lic.get("spec", {}).get("gateway", {}).get("baseDomain") or "k8s.orb.local"
            )
            dirty = False

            # 1. If the previous active instance lost the license, flip its old 'Instance Live' embed to Standby
            if active_msg and active_instance != active_msg.get("instance"):
                await self._demote_active_message(bot, active_msg, license_name, active_instance, now_ts)
                state["active_msg"] = None
                active_msg = None
                dirty = True

            # 2. Reconcile any in-flight pending switch/deactivate message
            if not pending:
                if dirty:
                    await self._persist_state(license_name, ns, state)
                return "idle"

            created_at = float(pending.get("created_at") or now_ts)
            elapsed = max(0.0, now_ts - created_at)
            action = pending.get("action", "activate")
            target = pending.get("instance", "")
            target_ns = pending.get("namespace") or ns

            if action == "activate":
                if active_instance == target:
                    # Target is now LIVE!
                    if active_msg and int(active_msg.get("message_id", 0)) != int(pending.get("message_id", 0)):
                        await self._demote_active_message(bot, active_msg, license_name, target, now_ts)

                    inst_data = await asyncio.to_thread(k8s.get_foundry_instance, target, target_ns)
                    url = f"https://{target}.{base_domain}"
                    if inst_data:
                        live_embed = format_instance_embed(inst_data, is_active_override=True)
                        live_embed.title = f"✅ Instance Live: {target}"
                    else:
                        live_embed = discord.Embed(
                            title=f"✅ Instance Live: {target}",
                            color=discord.Color.green(),
                        )
                    live_embed.description = (
                        f"Switch complete! The instance is now accessible.\n"
                        f"🔗 [**Access Instance**]({url})"
                    )

                    edit_res = await self._edit_message(bot, pending, live_embed, now_ts)
                    was_queued = bool(pending.get("was_queued"))

                    if edit_res != "not_found":
                        state["active_msg"] = {
                            "channel_id": pending.get("channel_id"),
                            "message_id": pending.get("message_id"),
                            "instance": target,
                            "user_id": pending.get("user_id"),
                            "namespace": target_ns,
                        }
                    else:
                        state["active_msg"] = None

                    state["pending_msg"] = None
                    await self._persist_state(license_name, ns, state)

                    if was_queued and pending.get("user_id"):
                        await self._send_completion_ping(
                            bot,
                            pending,
                            f"<@{pending['user_id']}> ✅ **{target}** is now active and ready!\n🔗 {url}",
                            now_ts,
                        )
                    self._webhooks.pop(int(pending.get("message_id", 0)), None)
                    return "completed"

                if requested_instance != target:
                    # Another command changed spec.activeInstanceName before target ever became active
                    dest = f"**{requested_instance}**" if requested_instance else "**standby**"
                    sup_embed = discord.Embed(
                        title="🚫 Switch Superseded",
                        description=(
                            f"Activation of **{target}** was superseded — license **{license_name}** "
                            f"is now targeting {dest}."
                        ),
                        color=discord.Color.light_grey(),
                    )
                    await self._edit_message(bot, pending, sup_embed, now_ts)
                    state["pending_msg"] = None
                    self._webhooks.pop(int(pending.get("message_id", 0)), None)
                    await self._persist_state(license_name, ns, state)
                    return "superseded"

            else:  # action == "deactivate"
                if active_instance != target:
                    standby_embed = discord.Embed(
                        title="🟠 Instance Standby",
                        description=f"Instance **{target}** is now on standby. Users will see the status page.",
                        color=discord.Color.orange(),
                    )
                    await self._edit_message(bot, pending, standby_embed, now_ts)
                    was_queued = bool(pending.get("was_queued"))
                    state["pending_msg"] = None
                    await self._persist_state(license_name, ns, state)
                    if was_queued and pending.get("user_id"):
                        await self._send_completion_ping(
                            bot,
                            pending,
                            f"<@{pending['user_id']}> 🟠 **{target}** is now on standby.",
                            now_ts,
                        )
                    self._webhooks.pop(int(pending.get("message_id", 0)), None)
                    return "completed"

                if requested_instance == target:
                    cancel_embed = discord.Embed(
                        title="🚫 Standby Cancelled",
                        description=f"Deactivation of **{target}** was cancelled because it was re-activated.",
                        color=discord.Color.blue(),
                    )
                    await self._edit_message(bot, pending, cancel_embed, now_ts)
                    state["pending_msg"] = None
                    self._webhooks.pop(int(pending.get("message_id", 0)), None)
                    await self._persist_state(license_name, ns, state)
                    return "superseded"

            # Still waiting for transition to finish
            if elapsed > MAX_PENDING_AGE_SECONDS:
                state["pending_msg"] = None
                self._webhooks.pop(int(pending.get("message_id", 0)), None)
                await self._persist_state(license_name, ns, state)
                return "idle"

            stage = self._classify_stage(blocked_reason, elapsed)
            prev_stage = pending.get("stage") or "switching"
            prev_reason = pending.get("last_reason") or ""
            last_edit_at = float(pending.get("last_edit_at") or 0.0)

            if stage != "switching" and not pending.get("was_queued"):
                pending["was_queued"] = True
                dirty = True

            should_edit = stage != "switching" and (
                stage != prev_stage
                or (blocked_reason != prev_reason and (now_ts - last_edit_at) >= PROGRESS_EDIT_INTERVAL_SECONDS)
            )
            if should_edit:
                prog_embed = self._build_queued_embed(
                    action=action,
                    target_instance=target,
                    license_name=license_name,
                    active_holder=active_instance,
                    blocked_reason=blocked_reason,
                    stage=stage,
                )
                edit_res = await self._edit_message(bot, pending, prog_embed, now_ts)
                if edit_res == "not_found":
                    state["pending_msg"] = None
                    self._webhooks.pop(int(pending.get("message_id", 0)), None)
                    await self._persist_state(license_name, ns, state)
                    return "idle"
                pending["stage"] = stage
                pending["last_reason"] = blocked_reason
                pending["last_edit_at"] = now_ts
                dirty = True

            if dirty:
                await self._persist_state(license_name, ns, state)

            return "queued" if pending.get("was_queued") else "waiting"

    async def reconcile_all(self, bot: Any) -> None:
        """Background sweep across all FoundryLicense resources."""
        if not k8s.is_connected():
            return
        licenses = await asyncio.to_thread(k8s.get_foundry_licenses, k8s.FOUNDRY_NAMESPACE, False)
        for lic in licenses:
            meta = lic.get("metadata", {}) or {}
            name = meta.get("name")
            if not name:
                continue
            ns = meta.get("namespace") or k8s.FOUNDRY_NAMESPACE
            key = self._key(ns, name)
            has_annot = bool((meta.get("annotations") or {}).get(ANNOTATION_DISCORD_TRACKED_MESSAGES))
            has_mem = key in self._state and (
                self._state[key].get("active_msg") or self._state[key].get("pending_msg")
            )
            if has_annot or has_mem:
                try:
                    await self.reconcile_license(bot, name, ns, lic_obj=lic)
                except Exception as e:
                    print(f"Error reconciling Discord messages for license {name}: {e}")


# Shared singleton used by /vtt-update and the background loop
switch_tracker = SwitchTracker()
