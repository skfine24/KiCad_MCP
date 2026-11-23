"""Path, revision and atomic-write helpers shared by all KiCad MCP tools.

The server frequently runs against Korean UNC paths.  Keeping every write
atomic and every edit guarded by a revision hash prevents the partial writes
and stale reads seen with in-place pcbnew saves.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_STAGE_DIR = Path(tempfile.gettempdir()) / "kicad-mcp" / "stage"
_LOCK_SUFFIX = ".kicad-mcp.lock"
_DEFAULT_STALE_SECONDS = 900.0


def normalize_path(path: str | Path) -> Path:
    """Return a resolved, NFC-normalized path.

    ``Path.resolve`` is used so callers always see the same spelling for a
    file regardless of how the path was typed.  NFC normalization keeps
    Korean directory names stable across Windows APIs.
    """
    text = unicodedata.normalize("NFC", str(path))
    return Path(text).expanduser().resolve()


def is_network_path(path: str | Path) -> bool:
    """True for UNC paths and mapped network drives on Windows."""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(text)[0]
    if not drive:
        return False
    try:
        # DRIVE_REMOTE == 4
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4
    except Exception:
        return False


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_revision(path: str | Path) -> dict:
    """mtime/size/hash snapshot used for stale-read and stale-write checks."""
    p = normalize_path(path)
    if not p.is_file():
        return {
            "path": str(p),
            "exists": False,
            "mtime_ns": None,
            "size": None,
            "sha256": None,
        }
    stat = p.stat()
    return {
        "path": str(p),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "size": stat.st_size,
        "sha256": sha256_file(p),
    }


def revision_matches(path: str | Path, expected: str | None) -> bool:
    if expected is None:
        return True
    actual = file_revision(path).get("sha256")
    return actual is not None and actual == expected


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 8) -> None:
    """os.replace with backoff for transient Windows/NAS sharing violations."""
    delay = 0.15
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            # WinError 5 (access denied) / 32 (sharing violation) are common on
            # SMB shares when antivirus or the KiCad GUI briefly holds a handle.
            if getattr(exc, "winerror", None) not in (5, 32) and not isinstance(exc, PermissionError):
                raise
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay)
            delay = min(delay * 1.7, 2.0)
    if last_error is not None:
        raise last_error


def atomic_write_text(
    path: str | Path,
    text: str,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> Path:
    """Write text through a same-directory temporary file and os.replace."""
    target = normalize_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    tmp = target.with_name(f".{target.name}.{token}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def atomic_copy(src: str | Path, dst: str | Path) -> Path:
    """Copy a file through a same-directory temporary file and os.replace."""
    source = normalize_path(src)
    target = normalize_path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    tmp = target.with_name(f".{target.name}.{token}.tmp")
    try:
        shutil.copy2(source, tmp)
        _replace_with_retry(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def backup_file(path: str | Path) -> str | None:
    """Create the timestamped ``.bak`` name used by earlier releases.

    A numeric suffix is added when several edits happen within the same
    second so an earlier backup is never overwritten.
    """
    target = normalize_path(path)
    if not target.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.name}.{stamp}_pre_edit.bak")
    counter = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.{stamp}_{counter}_pre_edit.bak")
        counter += 1
    atomic_copy(target, backup)
    return str(backup)


def stage_local(path: str | Path, suffix: str | None = None) -> Path:
    """Copy a file to the local temp staging directory.

    The sibling ``.kicad_pro`` is copied alongside the staged document (with a
    matching stem) so pcbnew/eeschema load the project's netclasses and design
    rules instead of falling back to built-in defaults.  Without this, a zone
    refill on a staged 4-layer board used 0.2 mm clearances and re-fragmented
    the GND plane.
    """
    source = normalize_path(path)
    _STAGE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = suffix if suffix is not None else source.suffix
    staging = _STAGE_DIR / f"{uuid.uuid4().hex}_{source.stem}{suffix}"
    shutil.copy2(source, staging)
    project = source.with_suffix(".kicad_pro")
    if project.is_file():
        shutil.copy2(project, staging.with_suffix(".kicad_pro"))
    return staging


def commit_staged(
    staging: str | Path,
    target: str | Path,
    backup: bool = True,
    expected_revision: str | None = None,
) -> dict:
    """Atomically replace ``target`` with ``staging`` after optional checks."""
    staging_path = normalize_path(staging)
    target_path = normalize_path(target)
    if not staging_path.is_file():
        raise FileNotFoundError(f"staging file not found: {staging_path}")
    if expected_revision and not revision_matches(target_path, expected_revision):
        raise RuntimeError(
            "revision mismatch: target changed since it was read "
            f"(expected {expected_revision})"
        )
    backup_path = backup_file(target_path) if backup else None
    atomic_copy(staging_path, target_path)
    try:
        staging_path.unlink()
    except OSError:
        pass
    return {
        "committed": True,
        "target": str(target_path),
        "backup": backup_path,
        "revision": file_revision(target_path),
    }


class ProjectLock:
    """Small cross-process lock beside a KiCad document.

    A lock file is created with O_EXCL.  A lock older than ``stale_seconds``
    is treated as abandoned and removed before retrying once.
    """

    def __init__(self, path: str | Path, stale_seconds: float = _DEFAULT_STALE_SECONDS):
        self.target = normalize_path(path)
        self.lock_path = self.target.with_name(self.target.name + _LOCK_SUFFIX)
        self.stale_seconds = stale_seconds
        self._fd: int | None = None

    def acquire(self) -> "ProjectLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                if attempt == 0 and self._is_stale():
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                raise RuntimeError(
                    f"project is locked by another process: {self.lock_path}"
                )
            payload = {
                "pid": os.getpid(),
                "created": time.time(),
                "created_iso": datetime.now().isoformat(timespec="seconds"),
                "target": str(self.target),
            }
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.fsync(fd)
            self._fd = fd
            return self
        raise RuntimeError(f"could not acquire lock: {self.lock_path}")

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except OSError:
            return True
        return age > self.stale_seconds

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.lock_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ProjectLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@contextmanager
def locked(path: str | Path, stale_seconds: float = _DEFAULT_STALE_SECONDS):
    lock = ProjectLock(path, stale_seconds=stale_seconds)
    try:
        lock.acquire()
        yield lock
    finally:
        lock.release()
