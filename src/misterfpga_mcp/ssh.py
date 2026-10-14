"""SSH to a MiSTer: a closed set of commands, and SFTP limited to a few paths.

What SSH is for here: MiSTer.ini (Zaparoo can't edit it, and mrext Remote can't
edit per-core sections), save backups, and three small reads Zaparoo doesn't
offer an unauthenticated client: the running core, free space, and whether
MiSTer Super Attract Mode is cycling games.

Why so locked down: the login is root, on a device that boots from that SD
card. So:
- **No shell for the model.** ``Command`` is the complete list of what runs
  remotely; each is a constant string. Nothing a tool receives is ever pasted
  into a command line.
- **SFTP paths are checked before every call** (``check_path``): reads only of
  MiSTer*.ini and the saves/savestates trees; writes only of MiSTer*.ini, its
  .tmp and this server's .bak files; deletes only of this server's .bak files.
  Anything else raises before a byte is sent.
- **The host key is pinned.** A host without ``ssh.host_key`` is never
  connected to; ``check --ssh`` shows the fingerprint and saves it once you've
  confirmed it. A changed key is refused, which is what you want if the
  address now belongs to something else.
- **Keys only.** MiSTer's stock root password is ``1``; it's never stored.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import posixpath
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from .config import SshSettings
from .errors import MisterError, Unavailable

log = logging.getLogger(__name__)

SD = "/media/fat"
CONNECT_TIMEOUT = 8.0
COMMAND_TIMEOUT = 10.0


class Command(enum.Enum):
    """Everything this server ever runs on a MiSTer. Constants only."""

    CORENAME = "cat /tmp/CORENAME"  # ASSUMPTION M-CORENAME
    PROCESSES = "ps -o args"  # ASSUMPTION S-ACTIVE (busybox ps; MiSTer_SAM parses the same output)
    FREE_SPACE = "df -k /media/fat"
    HOSTNAME = "hostname"
    MENU = "echo load_core /media/fat/menu.rbf > /dev/MiSTer_cmd"  # ASSUMPTION M-CMD


# MiSTer.ini and its alternates (MiSTer_<name>.ini), never MiSTer_example.ini.
INI_NAME = re.compile(r"MiSTer(_[A-Za-z0-9 _-]{1,40})?\.ini")
_INI_PATH = re.compile(rf"{SD}/(MiSTer(?:_[A-Za-z0-9 _-]{{1,40}})?\.ini)")
_INI_TMP = re.compile(rf"{SD}/MiSTer(?:_[A-Za-z0-9 _-]{{1,40}})?\.ini\.tmp")
# This server's backups: MiSTer.ini.20261013T201500Z.bak
_INI_BAK = re.compile(rf"{SD}/MiSTer(?:_[A-Za-z0-9 _-]{{1,40}})?\.ini\.\d{{8}}T\d{{6}}Z\.bak")
SAVE_ROOTS = (f"{SD}/saves", f"{SD}/savestates")  # ASSUMPTION M-PATHS

Purpose = str  # "read", "write" or "remove"


def is_ini_name(name: str) -> bool:
    return bool(INI_NAME.fullmatch(name)) and name.lower() != "mister_example.ini"


def check_path(path: str, purpose: Purpose) -> str:
    """Return ``path`` if this server may use it for ``purpose``; raise otherwise.

    The path must already be normalized (no "..", no "//", no trailing "/"), so
    a crafted name can't escape the allowed trees by resolving outside them.
    """
    if posixpath.normpath(path) != path or not path.startswith(SD + "/") or "\x00" in path:
        raise MisterError(f"refusing path {path!r}: not a plain path under {SD}")
    ini = _INI_PATH.fullmatch(path)
    if purpose == "read":
        ok = bool(ini and is_ini_name(ini.group(1))) or any(path == r or path.startswith(r + "/") for r in SAVE_ROOTS)
        ok = ok or bool(_INI_BAK.fullmatch(path))
    elif purpose == "write":
        ok = bool(ini and is_ini_name(ini.group(1))) or bool(_INI_TMP.fullmatch(path) or _INI_BAK.fullmatch(path))
    elif purpose == "remove":
        ok = bool(_INI_BAK.fullmatch(path))
    else:
        ok = False
    if not ok:
        raise MisterError(f"refusing to {purpose} {path}: outside what this server may touch")
    return path


@dataclass(frozen=True)
class RemoteFile:
    path: str  # absolute, on the MiSTer
    size: int
    mtime: int  # seconds since the epoch


def fingerprint(public_key: str) -> str:
    """SHA256 fingerprint of an OpenSSH public key line, as `ssh-keygen -l` prints it."""
    return str(asyncssh.import_public_key(public_key).get_fingerprint("sha256"))


class MisterShell:
    """One MiSTer's SSH connection: opened on first use, reopened after a failure."""

    def __init__(self, label: str, host: str, settings: SshSettings) -> None:
        self.label = label
        self.host = host
        self.settings = settings
        self.error: str | None = None
        self._conn: asyncssh.SSHClientConnection | None = None
        self._sftp: asyncssh.SFTPClient | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _not_ready(self) -> str | None:
        if not self.settings.key:
            return f"SSH to {self.label} has no key set (ssh.key in config.json). Run ssh-copy-id, then check --ssh."
        if not self.settings.host_key:
            return (
                f"SSH to {self.label} has no pinned host key yet. Run "
                f"`mcp-server-misterfpga check --host {self.host} --ssh` to confirm and save it."
            )
        return None

    async def _connect(self) -> asyncssh.SSHClientConnection:
        if self._conn is not None:
            return self._conn
        if (why := self._not_ready()) is not None:
            self.error = why
            raise Unavailable(why)
        assert self.settings.host_key is not None and self.settings.key is not None
        try:
            trusted = [asyncssh.import_public_key(self.settings.host_key)]
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._conn = await asyncssh.connect(
                    self.host,
                    port=self.settings.port,
                    username=self.settings.user,
                    client_keys=[self.settings.key],
                    known_hosts=(trusted, [], []),
                    agent_path=None,
                    config=None,
                )
        except asyncssh.HostKeyNotVerifiable:
            self.error = (
                f"{self.label}'s SSH host key doesn't match the one pinned in config.json. If you reflashed or "
                "replaced this MiSTer, remove ssh.host_key and run check --ssh again; otherwise something else "
                "may be answering at this address."
            )
        except asyncssh.PermissionDenied:
            self.error = (
                f"{self.label} refused the SSH key for {self.settings.user}. Run "
                f"ssh-copy-id -i {self.settings.key}.pub {self.settings.user}@{self.host} (MiSTer's stock password "
                "is 1)."
            )
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            self.error = f"Can't reach {self.label} over SSH: {exc.__class__.__name__}: {exc}"
        else:
            self.error = None
            return self._conn
        log.info("%s: %s", self.label, self.error)
        raise Unavailable(self.error)

    async def close(self) -> None:
        conn, self._conn, self._sftp = self._conn, None, None
        if conn is not None:
            conn.close()
            await conn.wait_closed()

    async def _reset(self, exc: BaseException) -> None:
        self.error = f"SSH to {self.label} failed: {exc.__class__.__name__}: {exc}"
        log.info("%s", self.error)
        await self.close()

    async def run(self, command: Command) -> str:
        """Run one of the fixed commands; return its stdout. A non-zero exit is an error."""
        async with self._lock:
            conn = await self._connect()
            try:
                result = await conn.run(command.value, check=False, timeout=COMMAND_TIMEOUT)
            except (OSError, asyncssh.Error, TimeoutError) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SSH failed") from None
        if result.exit_status != 0:
            err = str(result.stderr or "").strip()
            raise MisterError(f"{command.name.lower()} failed on {self.label}: {err or f'exit {result.exit_status}'}")
        return str(result.stdout or "")

    async def _sftp_client(self) -> asyncssh.SFTPClient:
        conn = await self._connect()
        if self._sftp is None:
            self._sftp = await conn.start_sftp_client()
        return self._sftp

    async def read(self, path: str) -> bytes:
        check_path(path, "read")
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                async with sftp.open(path, "rb") as f:
                    data = await f.read()
            except asyncssh.SFTPNoSuchFile:
                raise MisterError(f"{path} doesn't exist on {self.label}") from None
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SFTP failed") from None
        return data if isinstance(data, bytes) else str(data).encode()

    async def exists(self, path: str) -> bool:
        check_path(path, "read")
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                return bool(await sftp.exists(path))
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SFTP failed") from None

    async def list_inis(self) -> list[str]:
        """The MiSTer*.ini files in the SD card's root (not MiSTer_example.ini), sorted."""
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                names = [str(n) for n in await sftp.listdir(SD)]
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SFTP failed") from None
        return sorted((n for n in names if is_ini_name(n)), key=lambda n: (n != "MiSTer.ini", n.lower()))

    async def write_replace(self, path: str, data: bytes, backup: str) -> None:
        """Copy ``path`` to ``backup``, write ``data`` to ``path``.tmp, then move it over ``path``.

        The backup is written first, so a failure at any later step leaves either
        the old file or the new one in place, plus the backup.
        """
        check_path(path, "write")
        check_path(backup, "write")
        tmp = check_path(path + ".tmp", "write")
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                if await sftp.exists(path):
                    async with sftp.open(path, "rb") as src:
                        old = await src.read()
                    async with sftp.open(backup, "wb") as dst:
                        await dst.write(old)
                async with sftp.open(tmp, "wb") as dst:
                    await dst.write(data)
                try:
                    await sftp.posix_rename(tmp, path)  # ASSUMPTION M-INI-RENAME
                except asyncssh.SFTPOpUnsupported:
                    # Plain SFTP rename won't replace an existing file. The backup already holds the old
                    # contents, so removing the original first loses nothing.
                    if await sftp.exists(path):
                        await sftp.remove(path)
                    await sftp.rename(tmp, path)
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(
                    f"Writing {path} on {self.label} failed ({exc}); the original is unchanged or saved as {backup}."
                ) from None

    async def remove(self, path: str) -> None:
        check_path(path, "remove")
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                await sftp.remove(path)
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SFTP failed") from None

    async def list_backups(self) -> list[str]:
        """This server's MiSTer*.ini.<timestamp>.bak files, oldest first."""
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                names = [str(n) for n in await sftp.listdir(SD)]
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SFTP failed") from None
        return sorted(f"{SD}/{n}" for n in names if _INI_BAK.fullmatch(f"{SD}/{n}"))

    async def walk(self, root: str, limit: int) -> AsyncIterator[RemoteFile]:
        """Every regular file under ``root`` (one of SAVE_ROOTS), depth first. Raises past ``limit`` files."""
        check_path(root, "read")
        seen = 0
        stack = [root]
        while stack:
            folder = stack.pop()
            async with self._lock:
                sftp = await self._sftp_client()
                try:
                    entries = [e async for e in sftp.scandir(folder)]
                except asyncssh.SFTPNoSuchFile:
                    continue
                except (OSError, asyncssh.Error) as exc:
                    await self._reset(exc)
                    raise Unavailable(self.error or "SFTP failed") from None
            for e in sorted(entries, key=lambda e: str(e.filename)):
                name = str(e.filename)
                if name in (".", ".."):
                    continue
                path = check_path(f"{folder}/{name}", "read")
                if e.attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                    stack.append(path)
                elif e.attrs.type == asyncssh.FILEXFER_TYPE_REGULAR:
                    seen += 1
                    if seen > limit:
                        raise MisterError(f"More than {limit} files under {root} on {self.label}; stopping there.")
                    yield RemoteFile(path, int(e.attrs.size or 0), int(e.attrs.mtime or 0))

    async def fetch(self, remote: RemoteFile, local: Path) -> None:
        """Copy one file down, keeping its mtime (so the next backup can compare)."""
        check_path(remote.path, "read")
        async with self._lock:
            sftp = await self._sftp_client()
            try:
                await sftp.get(remote.path, str(local), preserve=True)
            except (OSError, asyncssh.Error) as exc:
                await self._reset(exc)
                raise Unavailable(self.error or "SFTP failed") from None


async def read_host_key(host: str, port: int) -> str:
    """The MiSTer's SSH host key as an OpenSSH line, for `check --ssh` to show and pin. No login happens."""
    async with asyncio.timeout(CONNECT_TIMEOUT):
        key = await asyncssh.get_server_host_key(host, port)
    if key is None:
        raise MisterError(f"{host}:{port} didn't offer an SSH host key")
    return str(key.export_public_key("openssh").decode().strip())
