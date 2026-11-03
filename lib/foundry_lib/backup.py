"""
Instance backup archiving, manifest generation, and HMAC-signed download tokens.

Designed to run inside the sidecar monitor container (where `/data` is mounted read-only)
with zero disk usage on Kubernetes nodes:
- Small archives (<= max_inline_bytes) are compressed in a bounded memory buffer for
  direct Discord DM file attachment.
- Larger archives abort the inline buffer immediately and stream on-demand over HTTP
  using non-seekable ZIP64 data descriptors with bounded memory backpressure.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
import re
import stat
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import zipfile

VALID_BACKUP_SCOPES: Tuple[str, ...] = ("default", "worlds", "full")
DEFAULT_DISCORD_MAX_BYTES: int = 9_500_000  # ~9.06 MiB (safe margin below Discord's 10 MiB DM limit)
DEFAULT_TOKEN_TTL_SECONDS: int = 3600  # 1 hour
DEFAULT_INLINE_CACHE_TTL_SECONDS: int = 180  # 3 minutes for bot pickup
MAX_CACHED_INLINE_BACKUPS: int = 3
STREAM_CHUNK_BYTES: int = 65536

EXCLUDED_DIR_NAMES = frozenset({"@eaDir", "#recycle", "__MACOSX"})
EXCLUDED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class BackupError(Exception):
    """Base error for backup operations."""


class BackupBufferLimitExceeded(BackupError):
    """Raised internally when compressed archive output exceeds max_inline_bytes."""


class BackupStreamCancelled(BackupError):
    """Raised when a streaming client disconnects mid-download."""


@dataclass(frozen=True)
class BackupFileEntry:
    abs_path: str
    arcname: str
    size_bytes: int
    mtime: float


@dataclass
class BackupPlan:
    instance_name: str
    scope: str
    created_at: str
    manifest: Dict[str, Any]
    files: List[BackupFileEntry]
    uncompressed_bytes: int


def sanitize_filename(name: str) -> str:
    """Sanitize a filename for safe use in URLs and Content-Disposition headers."""
    cleaned = _SAFE_FILENAME_RE.sub("_", name.strip()).strip("._")
    return cleaned or "foundry-backup.zip"


def build_backup_filename(
    instance_name: str,
    scope: str = "default",
    now: Optional[datetime] = None,
) -> str:
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ts = dt.strftime("%Y%m%d-%H%M%SZ")
    suffix = "backup" if scope == "default" else f"{scope}-backup"
    return sanitize_filename(f"{instance_name}-{suffix}-{ts}.zip")


def _is_within_root(candidate_realpath: str, root_realpath: str) -> bool:
    if candidate_realpath == root_realpath:
        return True
    prefix = root_realpath.rstrip(os.sep) + os.sep
    return candidate_realpath.startswith(prefix)


def _mtime_to_zip_tuple(mtime: float) -> Tuple[int, int, int, int, int, int]:
    """Convert POSIX mtime to a valid ZIP timestamp tuple (clamped to 1980..2107)."""
    try:
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return (1980, 1, 1, 0, 0, 0)
    if dt.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if dt.year > 2107:
        return (2107, 12, 31, 23, 59, 58)
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _read_package_summaries(
    pkg_dir: str,
    real_data_dir: str,
    json_filename: str,
    extra_keys: Tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
    if not os.path.isdir(pkg_dir) or os.path.islink(pkg_dir):
        return []
    real_pkg_dir = os.path.realpath(pkg_dir)
    if not _is_within_root(real_pkg_dir, real_data_dir):
        return []

    summaries: List[Dict[str, Any]] = []
    try:
        entries = sorted(os.listdir(pkg_dir))
    except OSError:
        return []

    for entry_name in entries:
        if entry_name in EXCLUDED_DIR_NAMES or entry_name.startswith(".nfs"):
            continue
        sub_dir = os.path.join(pkg_dir, entry_name)
        if os.path.islink(sub_dir) or not os.path.isdir(sub_dir):
            continue
        manifest_path = os.path.join(sub_dir, json_filename)
        if os.path.islink(manifest_path) or not os.path.isfile(manifest_path):
            continue
        if not _is_within_root(os.path.realpath(manifest_path), real_data_dir):
            continue

        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            summaries.append({"id": entry_name})
            continue

        if not isinstance(raw, dict):
            summaries.append({"id": entry_name})
            continue

        item: Dict[str, Any] = {
            "id": str(raw.get("id") or raw.get("name") or entry_name),
        }
        if raw.get("title"):
            item["title"] = str(raw["title"])
        if raw.get("version") is not None:
            item["version"] = str(raw["version"])
        if raw.get("manifest"):
            item["manifest"] = str(raw["manifest"])
        for k in extra_keys:
            if raw.get(k) is not None:
                item[k] = raw[k]
        summaries.append(item)

    return summaries


def collect_backup_plan(
    data_root: str,
    instance_name: str,
    scope: str = "default",
    foundry_version: Optional[str] = None,
    now: Optional[datetime] = None,
) -> BackupPlan:
    """
    Scan `<data_root>/Data` according to `scope` and build a deterministic BackupPlan.

    Security invariants:
    - Never reads `<data_root>/Config` (protects license.json and admin.txt),
      `<data_root>/container_cache`, or `<data_root>/Logs`.
    - Never follows symlinks and rejects any path resolving outside `<data_root>/Data`.
    """
    if scope not in VALID_BACKUP_SCOPES:
        raise ValueError(f"Unsupported backup scope '{scope}'. Must be one of {VALID_BACKUP_SCOPES}.")

    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created_at = dt.isoformat().replace("+00:00", "Z")
    data_dir = os.path.join(data_root, "Data")

    worlds_summary: List[Dict[str, Any]] = []
    systems_summary: List[Dict[str, Any]] = []
    modules_summary: List[Dict[str, Any]] = []
    files: List[BackupFileEntry] = []
    total_bytes = 0

    if os.path.isdir(data_dir) and not os.path.islink(data_dir):
        real_data_dir = os.path.realpath(data_dir)
        worlds_summary = _read_package_summaries(
            os.path.join(data_dir, "worlds"),
            real_data_dir,
            "world.json",
            extra_keys=("system", "coreVersion", "systemVersion"),
        )
        systems_summary = _read_package_summaries(
            os.path.join(data_dir, "systems"),
            real_data_dir,
            "system.json",
        )
        modules_summary = _read_package_summaries(
            os.path.join(data_dir, "modules"),
            real_data_dir,
            "module.json",
        )

        for root, dirs, filenames in os.walk(data_dir, topdown=True, followlinks=False):
            rel_root = os.path.relpath(root, data_dir)

            # Filter and sort subdirectories in-place
            kept_dirs: List[str] = []
            for d in sorted(dirs):
                if d in EXCLUDED_DIR_NAMES or d.startswith(".nfs"):
                    continue
                if rel_root == ".":
                    if scope == "default" and d in ("systems", "modules"):
                        continue
                    if scope == "worlds" and d != "worlds":
                        continue
                full_d = os.path.join(root, d)
                if os.path.islink(full_d):
                    continue
                if not _is_within_root(os.path.realpath(full_d), real_data_dir):
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs

            if rel_root == "." and scope == "worlds":
                # Strict worlds-only backup excludes loose files directly under Data/
                continue

            for fn in sorted(filenames):
                if fn in EXCLUDED_FILE_NAMES or fn.startswith(".nfs"):
                    continue
                full_f = os.path.join(root, fn)
                if os.path.islink(full_f):
                    continue
                if not _is_within_root(os.path.realpath(full_f), real_data_dir):
                    continue
                try:
                    st = os.lstat(full_f)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue

                rel_file = fn if rel_root == "." else os.path.join(rel_root, fn)
                rel_posix = rel_file.replace(os.sep, "/")
                arcname = f"Data/{rel_posix}"
                files.append(
                    BackupFileEntry(
                        abs_path=full_f,
                        arcname=arcname,
                        size_bytes=st.st_size,
                        mtime=st.st_mtime,
                    )
                )
                total_bytes += st.st_size

    manifest: Dict[str, Any] = {
        "instance": instance_name,
        "scope": scope,
        "createdAt": created_at,
        "foundryVersion": foundry_version,
        "counts": {
            "files": len(files),
            "uncompressedBytes": total_bytes,
            "worlds": len(worlds_summary),
            "systems": len(systems_summary),
            "modules": len(modules_summary),
        },
        "worlds": worlds_summary,
        "systems": systems_summary,
        "modules": modules_summary,
    }

    return BackupPlan(
        instance_name=instance_name,
        scope=scope,
        created_at=created_at,
        manifest=manifest,
        files=files,
        uncompressed_bytes=total_bytes,
    )


class NonSeekableBoundedBuffer(io.RawIOBase):
    """In-memory non-seekable writer that aborts immediately if output exceeds max_bytes."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._chunks: List[bytes] = []
        self._pos = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def write(self, b: Any) -> int:
        data = bytes(b)
        new_pos = self._pos + len(data)
        if new_pos > self.max_bytes:
            self._chunks.clear()
            raise BackupBufferLimitExceeded(
                f"Compressed backup exceeded inline limit of {self.max_bytes} bytes"
            )
        self._chunks.append(data)
        self._pos = new_pos
        return len(data)

    def getvalue(self) -> bytes:
        return b"".join(self._chunks)


def write_backup_zip(
    plan: BackupPlan,
    writer: Any,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> int:
    """
    Write a ZIP64 archive for `plan` into `writer` (seekable or non-seekable).

    Resilient against transient file disappearances (e.g. live LevelDB compaction)
    so the resulting ZIP central directory is always valid.
    """
    try:
        created_dt = datetime.fromisoformat(plan.created_at.replace("Z", "+00:00"))
        manifest_mtime = created_dt.timestamp()
    except ValueError:
        manifest_mtime = time.time()

    manifest_bytes = (json.dumps(plan.manifest, indent=2, sort_keys=False) + "\n").encode("utf-8")

    with zipfile.ZipFile(
        writer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as zf:
        manifest_info = zipfile.ZipInfo(
            filename="backup-manifest.json",
            date_time=_mtime_to_zip_tuple(manifest_mtime),
        )
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o100644 << 16
        with zf.open(manifest_info, mode="w") as dest:
            dest.write(manifest_bytes)

        for entry in plan.files:
            if cancel_check and cancel_check():
                raise BackupStreamCancelled("Backup stream cancelled by client")

            try:
                src_fh = open(entry.abs_path, "rb")
            except OSError:
                # Transient file removed mid-snapshot (e.g. LevelDB compaction)
                continue

            with src_fh:
                zinfo = zipfile.ZipInfo(
                    filename=entry.arcname,
                    date_time=_mtime_to_zip_tuple(entry.mtime),
                )
                zinfo.compress_type = zipfile.ZIP_DEFLATED
                zinfo.external_attr = 0o100644 << 16
                force_zip64 = entry.size_bytes >= 0x7FFFFFFF

                with zf.open(zinfo, mode="w", force_zip64=force_zip64) as dest:
                    while True:
                        if cancel_check and cancel_check():
                            raise BackupStreamCancelled("Backup stream cancelled by client")
                        try:
                            chunk = src_fh.read(STREAM_CHUNK_BYTES)
                        except OSError:
                            break
                        if not chunk:
                            break
                        dest.write(chunk)

    return writer.tell()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * ((4 - (len(text) % 4)) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def sign_backup_token(
    *,
    signing_key: str,
    instance_name: str,
    scope: str,
    filename: str,
    expires_at: int,
) -> str:
    """Create a compact HMAC-SHA256 signed download token bound to instance, scope, filename, and expiry."""
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    payload = {
        "inst": instance_name,
        "scope": scope,
        "fn": filename,
        "exp": int(expires_at),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(signing_key.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return f"v1.{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"


def verify_backup_token(
    token: str,
    *,
    candidate_keys: Sequence[str],
    expected_instance: str,
    expected_filename: Optional[str] = None,
    now_ts: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Verify an HMAC-signed backup download token.

    Returns `(payload_dict, "ok")` if valid, or `(None, reason)` where `reason` is
    `"expired"`, `"unavailable"`, or `"invalid"`.
    """
    valid_keys = [k for k in candidate_keys if k]
    if not valid_keys:
        return None, "unavailable"

    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None, "invalid"

    try:
        payload_bytes = _b64url_decode(parts[1])
        provided_sig = _b64url_decode(parts[2])
    except Exception:
        return None, "invalid"

    matched = False
    for key in valid_keys:
        expected_sig = hmac.new(key.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
        if hmac.compare_digest(provided_sig, expected_sig):
            matched = True
            break

    if not matched:
        return None, "invalid"

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None, "invalid"

    if not isinstance(payload, dict):
        return None, "invalid"

    if payload.get("inst") != expected_instance:
        return None, "invalid"

    scope = payload.get("scope")
    if scope not in VALID_BACKUP_SCOPES:
        return None, "invalid"

    if expected_filename is not None and payload.get("fn") != expected_filename:
        return None, "invalid"

    exp = payload.get("exp")
    if not isinstance(exp, int):
        return None, "invalid"

    now_val = time.time() if now_ts is None else now_ts
    if now_val > exp:
        return None, "expired"

    return payload, "ok"


class BackupManager:
    """Coordinates backup preparation, short-lived inline cache, and streaming concurrency."""

    def __init__(
        self,
        *,
        instance_name: str,
        data_root: str = "/data",
        key_provider: Callable[[], Sequence[str]],
        foundry_version: Optional[str] = None,
        token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        inline_cache_ttl_seconds: int = DEFAULT_INLINE_CACHE_TTL_SECONDS,
        max_concurrent_streams: int = 2,
    ):
        self.instance_name = instance_name
        self.data_root = data_root
        self._key_provider = key_provider
        self.foundry_version = foundry_version
        self.token_ttl_seconds = token_ttl_seconds
        self.inline_cache_ttl_seconds = inline_cache_ttl_seconds
        self.max_concurrent_streams = max_concurrent_streams

        self._lock = threading.Lock()
        # token -> (expires_monotonic, zip_bytes)
        self._inline_cache: Dict[str, Tuple[float, bytes]] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None

    def get_stream_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent_streams)
        return self._semaphore

    def get_keys(self) -> List[str]:
        seen: List[str] = []
        for k in self._key_provider() or ():
            val = (k or "").strip()
            if val and val not in seen:
                seen.append(val)
        return seen

    def verify_bearer_auth(self, auth_header: str) -> Tuple[bool, str]:
        """Verify `Authorization: Bearer <adminPassword>` header."""
        keys = self.get_keys()
        if not keys:
            return False, "unavailable"

        hdr = (auth_header or "").strip()
        if not hdr.lower().startswith("bearer "):
            return False, "unauthorized"
        provided = hdr[7:].strip()
        if not provided:
            return False, "unauthorized"

        for k in keys:
            if hmac.compare_digest(provided.encode("utf-8"), k.encode("utf-8")):
                return True, "ok"
        return False, "unauthorized"

    def _prune_inline_cache_locked(self, now_mono: float) -> None:
        expired = [tok for tok, (exp, _) in self._inline_cache.items() if now_mono >= exp]
        for tok in expired:
            del self._inline_cache[tok]
        while len(self._inline_cache) >= MAX_CACHED_INLINE_BACKUPS:
            oldest_tok = min(self._inline_cache.items(), key=lambda kv: kv[1][0])[0]
            del self._inline_cache[oldest_tok]

    def store_cached_inline(self, token: str, data: bytes) -> None:
        now_mono = time.monotonic()
        with self._lock:
            self._prune_inline_cache_locked(now_mono)
            self._inline_cache[token] = (now_mono + self.inline_cache_ttl_seconds, data)

    def pop_cached_inline(self, token: str) -> Optional[bytes]:
        now_mono = time.monotonic()
        with self._lock:
            entry = self._inline_cache.pop(token, None)
            self._prune_inline_cache_locked(now_mono)
        if entry is None:
            return None
        exp_mono, data = entry
        if now_mono >= exp_mono:
            return None
        return data

    def verify_download_token(
        self,
        token: str,
        filename: str,
        now_ts: Optional[float] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        return verify_backup_token(
            token,
            candidate_keys=self.get_keys(),
            expected_instance=self.instance_name,
            expected_filename=filename,
            now_ts=now_ts,
        )

    def prepare_backup(
        self,
        scope: str = "default",
        max_inline_bytes: int = DEFAULT_DISCORD_MAX_BYTES,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Scan `/data/Data`, generate a signed download token, and attempt bounded inline
        compression up to `max_inline_bytes`.
        """
        keys = self.get_keys()
        if not keys:
            raise BackupError("Instance credentials are not initialized yet")

        dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        plan = collect_backup_plan(
            data_root=self.data_root,
            instance_name=self.instance_name,
            scope=scope,
            foundry_version=self.foundry_version,
            now=dt,
        )
        filename = build_backup_filename(self.instance_name, scope=scope, now=dt)
        expires_at = int(dt.timestamp()) + int(self.token_ttl_seconds)
        token = sign_backup_token(
            signing_key=keys[0],
            instance_name=self.instance_name,
            scope=scope,
            filename=filename,
            expires_at=expires_at,
        )

        delivery = "link"
        compressed_bytes: Optional[int] = None

        if max_inline_bytes > 0:
            buf = NonSeekableBoundedBuffer(max_inline_bytes)
            try:
                write_backup_zip(plan, buf)
            except BackupBufferLimitExceeded:
                delivery = "link"
            else:
                zip_bytes = buf.getvalue()
                compressed_bytes = len(zip_bytes)
                delivery = "inline"
                self.store_cached_inline(token, zip_bytes)

        world_titles = [
            str(w.get("title") or w.get("id"))
            for w in plan.manifest.get("worlds", [])
            if w.get("title") or w.get("id")
        ]

        return {
            "instance": self.instance_name,
            "scope": scope,
            "delivery": delivery,
            "token": token,
            "filename": filename,
            "expiresAt": expires_at,
            "ttlSeconds": self.token_ttl_seconds,
            "fileCount": len(plan.files),
            "uncompressedBytes": plan.uncompressed_bytes,
            "compressedBytes": compressed_bytes,
            "worlds": world_titles,
            "worldsCount": len(plan.manifest.get("worlds", [])),
            "systemsCount": len(plan.manifest.get("systems", [])),
            "modulesCount": len(plan.manifest.get("modules", [])),
        }
