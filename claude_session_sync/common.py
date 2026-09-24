"""Shared helpers: hashing, atomic file operations, JSON formatting, ids, time."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

TOOL_NAME = "claude-session-sync"
TOOL_VERSION = "1.0.0"

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # same pattern Claude Desktop enforces for session ids
LOCAL_PREFIX = "local_"
TOMBSTONE_PREFIX = "deleted_"


def is_uuid(s: str) -> bool:
    return bool(UUID_RE.match(s or ""))


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def fmt_ms(ms: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ms) / 1000.0))
    except Exception:
        return "-"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str | os.PathLike, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dumps_record(obj: Any) -> bytes:
    """Serialise the way Claude Desktop does (compact JSON, UTF-8, no BOM, no trailing newline)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_json(path: str | os.PathLike) -> Any:
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def norm_path(p: str | os.PathLike | None) -> str:
    """Case-insensitive, separator-insensitive comparison key for Windows paths."""
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(str(p)))


def encode_project_dir(cwd: str) -> str:
    """Claude Code's project-directory encoding (every non-alphanumeric char becomes '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ---- file attributes (Windows) -------------------------------------------------

def _get_attrs(path: str) -> int | None:
    if sys.platform != "win32":
        return None
    v = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    return None if v == 0xFFFFFFFF else int(v)


def _set_attrs(path: str, attrs: int) -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs)


def _fsync_dir(path: str) -> None:
    # Directory fsync is not supported on Windows; NTFS journals metadata. No-op there.
    if sys.platform != "win32":
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def atomic_write(path: str | os.PathLike, data: bytes, *, overwrite: bool, keep_attrs_from: str | None = None) -> None:
    """Write ``data`` to ``path`` via temp file + rename.

    overwrite=False  -> fails with FileExistsError if the destination exists (no clobber).
    overwrite=True   -> atomically replaces (os.replace / MoveFileEx REPLACE_EXISTING).
    The temp file name never starts with ``local_`` / ``deleted_`` so Claude Desktop ignores it.
    """
    path = str(path)
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".cssync-%s.tmp" % secrets.token_hex(6))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if keep_attrs_from and os.path.exists(keep_attrs_from):
            a = _get_attrs(keep_attrs_from)
            if a is not None:
                _set_attrs(tmp, a & ~0x400)  # never propagate REPARSE_POINT
        if overwrite:
            os.replace(tmp, path)
        else:
            if os.path.exists(path):
                raise FileExistsError(path)
            os.rename(tmp, path)  # fails on Windows if it appeared in the meantime
        tmp = ""
        _fsync_dir(d)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def copy_file_exact(src: str, dst: str) -> None:
    """Copy bytes + timestamps, creating parent dirs (used for backups/restores)."""
    import shutil
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def walk_files(root: str | os.PathLike) -> Iterable[str]:
    for r, _ds, fs in os.walk(root):
        for f in fs:
            yield os.path.join(r, f)


def new_uuid() -> str:
    return str(uuid.uuid4())


def short(s: str | None, n: int = 8) -> str:
    return (s or "")[:n]


def human_size(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


class ToolError(Exception):
    """A user-facing, expected failure (bad input, unsafe state)."""
