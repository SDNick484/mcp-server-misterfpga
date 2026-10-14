"""A fake MiSTer SSH server: real SSH and SFTP (asyncssh), backed by a folder standing in for the SD card.

- Login is by public key only, and the server has its own host key, so the
  client's host-key pinning is exercised for real.
- Commands: exactly the ones in ssh.Command are answered, from the shared
  FakeDevice (core name, attract mode). Anything else exits 127 and is
  recorded in ``rejected``, so a test fails if the server ever sends something
  new.
- SFTP is chrooted to the device's ``sd`` folder, so /media/fat/MiSTer.ini is
  ``<sd>/media/fat/MiSTer.ini``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from pathlib import Path

import asyncssh

from ..ssh import Command
from .device import MENU, FakeDevice

log = logging.getLogger(__name__)


def new_key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519")


class FakeMister:
    def __init__(
        self,
        device: FakeDevice,
        host_key: asyncssh.SSHKey,
        client_public_key: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.device = device
        self.host_key = host_key
        self.client_public_key = client_public_key
        self.host = host
        self.port = port
        self.commands: list[str] = []
        self.rejected: list[str] = []
        self.sftp_posix_rename = True  # False: behave like an SFTP server without the OpenSSH extension
        self._server: asyncssh.SSHAcceptor | None = None

    @property
    def public_host_key(self) -> str:
        return str(self.host_key.export_public_key("openssh").decode().strip())

    async def start(self) -> int:
        fake = self

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True

            def password_auth_supported(self) -> bool:
                return False

        class Sftp(asyncssh.SFTPServer):
            def __init__(self, chan: asyncssh.SSHServerChannel[bytes]) -> None:
                super().__init__(chan, chroot=str(fake.device.sd).encode())

            def posix_rename(self, oldpath: bytes, newpath: bytes) -> Awaitable[None] | None:
                if not fake.sftp_posix_rename:
                    raise asyncssh.SFTPOpUnsupported("posix-rename not supported")
                return super().posix_rename(oldpath, newpath)

        self._server = await asyncssh.listen(
            self.host,
            self.port,
            server_factory=Server,
            server_host_keys=[self.host_key],
            authorized_client_keys=asyncssh.import_authorized_keys(self.client_public_key),
            process_factory=self._process,
            sftp_factory=Sftp,
            allow_scp=False,
        )
        self.port = self._server.get_port()
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def answer(self, command: str) -> tuple[str, int]:
        d = self.device
        self.commands.append(command)
        if command == Command.CORENAME.value:
            return d.core + "\n", 0
        if command == Command.PROCESSES.value:
            procs = ["PID   USER     TIME  COMMAND", "/sbin/init", "/media/fat/MiSTer", "sshd: root@notty"]
            if d.attract:
                procs.append("/bin/bash /tmp/.SAM_tmp/MiSTer_SAM_on.sh loop_core")
            procs.append("ps -o args")
            return "\n".join(procs) + "\n", 0
        if command == Command.FREE_SPACE.value:
            return (
                "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
                "/dev/mmcblk0p1       124703232  80000000  44703232  64% /media/fat\n",
                0,
            )
        if command == Command.HOSTNAME.value:
            return "MiSTer\n", 0
        if command == Command.MENU.value:
            d.to_menu()
            return "", 0
        self.rejected.append(command)
        return f"sh: {command.split()[0] if command.split() else ''}: not allowed by the simulator\n", 127

    def _process(self, process: asyncssh.SSHServerProcess[str]) -> None:
        out, status = self.answer(process.command or "")
        if status == 0:
            process.stdout.write(out)
        else:
            process.stderr.write(out)
        process.exit(status)


def menu_core() -> str:
    return MENU


def sd_path(device: FakeDevice, path: str) -> Path:
    """Where a MiSTer path lives in the simulator's SD folder: /media/fat/x -> <sd>/media/fat/x."""
    return device.sd / path.lstrip("/")
