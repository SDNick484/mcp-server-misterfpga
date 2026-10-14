"""Save backups: pull a MiSTer's saves and save states into dated snapshots on this machine.

Layout, under the configured ``backup_dir`` (there is no default; you say where):

    <backup_dir>/<host>/2026-10-13_201500/saves/SNES/Super Metroid (USA).sav
    <backup_dir>/<host>/2026-10-13_201500/savestates/...

Each snapshot is complete on its own, but unchanged files cost nothing: a file
whose size and modification time match the previous snapshot's copy is
hard-linked to it instead of downloaded, the way ``rsync --link-dest`` works.
So you can restore from any snapshot by copying one folder back, and keeping
30 of them costs roughly one copy plus what changed.

A snapshot is written as ``<name>.partial`` and renamed when complete, so a run
that fails halfway never looks like a good backup. Old snapshots beyond
``backup_keep`` are deleted, oldest first; nothing else in ``backup_dir`` is
touched. The MiSTer itself is only ever read.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import MisterError
from .ssh import SAVE_ROOTS, SD, MisterShell

MAX_FILES = 50_000
MAX_BYTES = 4 * 1024**3
MAX_SECONDS = 15 * 60
SNAPSHOT = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(-\d{1,3})?")

Progress = Callable[[int, str], Awaitable[None]]


@dataclass
class BackupResult:
    snapshot: str | None  # the folder written; None in a dry run
    files: int = 0
    copied: int = 0
    linked: int = 0
    bytes_copied: int = 0
    pruned: int = 0


def _size(n: int) -> str:
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= size:
            return f"{n / size:.0f} {unit}"
    return f"{n} bytes"


def slug(name: str) -> str:
    """A host name as a folder or MQTT topic segment: "Family Room" -> "family_room"."""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "mister"


def snapshots(host_dir: Path) -> list[Path]:
    if not host_dir.is_dir():
        return []
    return sorted(p for p in host_dir.iterdir() if p.is_dir() and SNAPSHOT.fullmatch(p.name))


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:  # a filesystem without hard links (some SMB mounts): fall back to a copy
        shutil.copy2(src, dst)


async def backup_saves(
    shell: MisterShell,
    host_name: str,
    backup_dir: Path,
    keep: int,
    *,
    dry_run: bool = False,
    progress: Progress | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> BackupResult:
    host_dir = backup_dir / slug(host_name)
    existing = snapshots(host_dir)
    previous = existing[-1] if existing else None
    stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
    name = stamp
    for n in range(2, 100):  # two backups in the same second: 2026-10-13_201500, then 2026-10-13_201500-2
        if not (host_dir / name).exists() and not (host_dir / f"{name}.partial").exists():
            break
        name = f"{stamp}-{n}"
    final = host_dir / name
    partial = host_dir / f"{name}.partial"
    if not dry_run:
        if not backup_dir.is_dir():
            raise MisterError(
                f"The backup folder {backup_dir} doesn't exist (or isn't mounted). Create or mount it, or point "
                "backup_dir / MISTERFPGA_BACKUP_DIR somewhere else."
            )
        partial.mkdir(parents=True)
    result = BackupResult(snapshot=None if dry_run else str(final))
    started = clock()
    try:
        for root in SAVE_ROOTS:  # ASSUMPTION M-PATHS
            async for f in shell.walk(root, MAX_FILES):
                if clock() - started > MAX_SECONDS:
                    raise MisterError(f"Backup of {host_name} took longer than {MAX_SECONDS // 60} minutes; stopped.")
                rel = f.path[len(SD) + 1 :]
                result.files += 1
                old = previous / rel if previous is not None else None
                unchanged = (
                    old is not None
                    and old.is_file()
                    and old.stat().st_size == f.size
                    and int(old.stat().st_mtime) == f.mtime
                )
                if unchanged:
                    result.linked += 1
                    if not dry_run:
                        assert old is not None
                        dest = partial / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        _link_or_copy(old, dest)
                else:
                    if result.bytes_copied + f.size > MAX_BYTES:
                        raise MisterError(
                            f"Backing up {host_name} would copy more than {_size(MAX_BYTES)} in one go; "
                            "stopped. Copy the first full backup another way, then let this keep it current."
                        )
                    result.copied += 1
                    result.bytes_copied += f.size
                    if not dry_run:
                        dest = partial / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        await shell.fetch(f, dest)
                if progress is not None and result.files % 100 == 0:
                    await progress(result.files, rel)
    except BaseException:
        if not dry_run:
            shutil.rmtree(partial, ignore_errors=True)
        raise
    if dry_run:
        return result
    partial.rename(final)
    for old_snapshot in snapshots(host_dir)[:-keep]:
        with contextlib.suppress(OSError):
            shutil.rmtree(old_snapshot)
            result.pruned += 1
    return result
