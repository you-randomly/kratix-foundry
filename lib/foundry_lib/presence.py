"""
Presence and contention state machine for the "Are you still there?" browser prompt.
Pure Python module with zero Kubernetes or network dependencies for deterministic testing.
"""

from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional, Tuple

from foundry_lib.activity import ActivityEvaluation

DEFAULT_CHALLENGE_TIMEOUT_SECONDS = 90.0
DEFAULT_ACK_SNOOZE_SECONDS = 900.0  # 15 minutes
DEFAULT_TAB_TTL_SECONDS = 15.0
DEFAULT_EMPTY_GRACE_SECONDS = 30.0


@dataclass
class ContentionDecision:
    """Outcome of evaluating contention against live activity and user acknowledgement."""
    should_interrupt: bool
    reasons: List[str] = field(default_factory=list)
    state: str = "none"


class PresenceCoordinator:
    """Coordinates browser tab heartbeats, contention challenges, and 15m snoozes."""

    def __init__(
        self,
        instance_name: str = "",
        challenge_timeout_seconds: float = DEFAULT_CHALLENGE_TIMEOUT_SECONDS,
        ack_snooze_seconds: float = DEFAULT_ACK_SNOOZE_SECONDS,
        tab_ttl_seconds: float = DEFAULT_TAB_TTL_SECONDS,
        empty_grace_seconds: float = DEFAULT_EMPTY_GRACE_SECONDS,
    ):
        self.instance_name = instance_name
        self.challenge_timeout_seconds = challenge_timeout_seconds
        self.ack_snooze_seconds = ack_snooze_seconds
        self.tab_ttl_seconds = tab_ttl_seconds
        self.empty_grace_seconds = empty_grace_seconds

        self.tab_heartbeats: Dict[str, float] = {}
        self.open_ws_count: int = 0
        self.connected_players: int = 0
        self.last_any_client_seen: Optional[float] = None

        self.contention_kind: Optional[str] = None  # "switch" | "standby" | "password_reset"
        self.target_instance: str = ""
        self.challenge_started_at: Optional[float] = None
        self.challenge_deadline: Optional[float] = None
        self.snoozed_until: Optional[float] = None
        self.release_now_requested: bool = False
        self.last_released_at: Optional[float] = None

    def _prune_tabs(self, now: float) -> None:
        cutoff = now - self.tab_ttl_seconds
        expired = [cid for cid, ts in self.tab_heartbeats.items() if ts < cutoff]
        for cid in expired:
            del self.tab_heartbeats[cid]

    def record_tab_heartbeat(self, client_id: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if client_id:
            self.tab_heartbeats[client_id] = now
            self.last_any_client_seen = now
        self._prune_tabs(now)

    def remove_tab(self, client_id: str) -> None:
        if client_id:
            self.tab_heartbeats.pop(client_id, None)

    def ws_opened(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.open_ws_count += 1
        self.last_any_client_seen = now

    def ws_closed(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.open_ws_count = max(0, self.open_ws_count - 1)
        if self.open_ws_count > 0:
            self.last_any_client_seen = now

    def active_tab_count(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        self._prune_tabs(now)
        return len(self.tab_heartbeats)

    def has_open_clients(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return self.active_tab_count(now) > 0 or self.open_ws_count > 0

    def clear_contention(self) -> None:
        self.contention_kind = None
        self.target_instance = ""
        self.challenge_started_at = None
        self.challenge_deadline = None
        self.snoozed_until = None
        self.release_now_requested = False

    def mark_released(self, now: Optional[float] = None) -> None:
        """Mark contention as released so open browser tabs see expired=True and reload cleanly."""
        now = time.time() if now is None else now
        self.last_released_at = now
        self.clear_contention()

    def acknowledge(self, now: Optional[float] = None) -> Dict[str, Any]:
        """User clicked 'I'm still here' — snooze contention for ack_snooze_seconds."""
        now = time.time() if now is None else now
        self.snoozed_until = now + self.ack_snooze_seconds
        self.challenge_started_at = None
        self.challenge_deadline = None
        self.release_now_requested = False
        self.last_released_at = None
        self.last_any_client_seen = now
        return self.get_status(now)

    def request_release(
        self,
        now: Optional[float] = None,
        connected_players: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """User clicked 'Release now' — only permitted when 0 players are connected in-world."""
        now = time.time() if now is None else now
        players = self.connected_players if connected_players is None else connected_players
        if players != 0:
            return False, "Cannot release immediately while players are connected in-world."
        self.release_now_requested = True
        self.snoozed_until = None
        self.challenge_deadline = now
        return True, "Release requested."

    @staticmethod
    def _format_duration_brief(seconds: int) -> str:
        if seconds >= 60:
            mins, secs = divmod(seconds, 60)
            return f"{mins}m {secs}s" if secs else f"{mins}m"
        return f"{seconds}s"

    def evaluate_contention(
        self,
        has_contention: bool,
        contention_kind: Optional[str],
        target_instance: str,
        activity_eval: ActivityEvaluation,
        now: Optional[float] = None,
    ) -> ContentionDecision:
        """
        Decide whether contention (license switch, standby, or pending password reset)
        may interrupt this instance right now.
        """
        now = time.time() if now is None else now
        self.connected_players = activity_eval.connected_players

        any_presence = (
            activity_eval.connected_players != 0
            or activity_eval.active_sessions > 0
            or self.has_open_clients(now)
        )
        if any_presence:
            self.last_any_client_seen = now

        if not has_contention:
            self.clear_contention()
            return ContentionDecision(
                should_interrupt=activity_eval.is_safe,
                reasons=list(activity_eval.reasons),
                state="none",
            )

        # If contention type changed (e.g. password_reset -> switch), keep snooze if active
        self.contention_kind = contention_kind
        self.target_instance = target_instance or ""

        # 1. Never interrupt active or settling package downloads
        if activity_eval.downloads_active or activity_eval.downloads_settling:
            dl_reasons = [r for r in activity_eval.reasons if "download" in r]
            return ContentionDecision(
                should_interrupt=False,
                reasons=dl_reasons or ["package download in progress"],
                state="download_blocked",
            )

        # 2. Honour user's explicit "Release now" click when 0 players are in-world
        if self.release_now_requested:
            if activity_eval.connected_players == 0:
                return ContentionDecision(
                    should_interrupt=True,
                    reasons=[],
                    state="released_by_user",
                )
            # Players joined before release executed; cancel immediate release
            self.release_now_requested = False

        # 3. Fast-path when nobody has Foundry open
        if not any_presence:
            if self.snoozed_until is not None and now < self.snoozed_until:
                # Brief grace window across page reloads (/setup -> /game) before clearing snooze
                if (
                    self.last_any_client_seen is None
                    or (now - self.last_any_client_seen) >= self.empty_grace_seconds
                ):
                    self.clear_contention()
                    return ContentionDecision(should_interrupt=True, reasons=[], state="empty")
                rem = int(max(1, round(self.snoozed_until - now)))
                return ContentionDecision(
                    should_interrupt=False,
                    reasons=[f"active session confirmed ({self._format_duration_brief(rem)} remaining)"],
                    state="snoozed",
                )
            self.clear_contention()
            return ContentionDecision(should_interrupt=True, reasons=[], state="empty")

        # 4. Someone is present — check active 15m snooze
        if self.snoozed_until is not None:
            if now < self.snoozed_until:
                rem = int(max(1, round(self.snoozed_until - now)))
                return ContentionDecision(
                    should_interrupt=False,
                    reasons=[f"active session confirmed ({self._format_duration_brief(rem)} remaining)"],
                    state="snoozed",
                )
            self.snoozed_until = None

        # 5. Start or check the 90s "Are you still there?" challenge
        if self.challenge_deadline is None:
            self.challenge_started_at = now
            self.challenge_deadline = now + self.challenge_timeout_seconds

        if now >= self.challenge_deadline:
            return ContentionDecision(
                should_interrupt=True,
                reasons=[],
                state="challenge_expired",
            )

        rem = int(max(1, round(self.challenge_deadline - now)))
        return ContentionDecision(
            should_interrupt=False,
            reasons=[f"awaiting user confirmation ({rem}s remaining)"],
            state="challenging",
        )

    def get_status(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Return JSON-serializable status payload for /__platform/status."""
        now = time.time() if now is None else now
        snoozed = bool(self.snoozed_until is not None and now < self.snoozed_until)
        challenging = bool(
            not snoozed
            and not self.release_now_requested
            and self.challenge_deadline is not None
            and now < self.challenge_deadline
        )
        recently_released = bool(
            self.last_released_at is not None and (now - self.last_released_at) < 15.0
        )
        expired = bool(
            recently_released
            or self.release_now_requested
            or (not snoozed and self.challenge_deadline is not None and now >= self.challenge_deadline)
        )
        remaining = (
            max(0, int(round(self.challenge_deadline - now)))
            if self.challenge_deadline is not None
            else 0
        )
        snooze_remaining = (
            max(0, int(round(self.snoozed_until - now)))
            if self.snoozed_until is not None
            else 0
        )
        return {
            "instance": self.instance_name,
            "challenging": challenging,
            "expired": expired,
            "remainingSeconds": remaining,
            "totalSeconds": int(self.challenge_timeout_seconds),
            "snoozed": snoozed,
            "snoozeRemainingSeconds": snooze_remaining,
            "contentionKind": self.contention_kind,
            "targetInstance": self.target_instance,
            "canReleaseNow": bool(self.connected_players == 0),
            "connectedPlayers": self.connected_players,
        }
