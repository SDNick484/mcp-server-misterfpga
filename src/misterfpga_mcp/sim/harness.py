"""Run simulated MiSTers (fake Zaparoo + fake SSH, one shared device each) and point a config at them.

Used by the tests and by `mcp-server-misterfpga simulate`, so both exercise the
same fakes. Each simulated MiSTer listens on its own loopback address
(127.0.0.1, 127.0.0.2, ...), which is how two real MiSTers look too: two
addresses, same ports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

from .device import FakeDevice
from .fake_mister import FakeMister, new_key
from .fake_zaparoo import FakeZaparoo

# Office is a MiSTer Pi; Family Room is a SuperStation One, simulated exactly like any MiSTer (ASSUMPTION H-SS1).
FIXTURES = ("office", "family_room")


@dataclass
class SimMister:
    fixture: str
    address: str
    device: FakeDevice
    zaparoo: FakeZaparoo
    ssh: FakeMister


@dataclass
class Simulation:
    root: Path  # holds sd/<name>/, config/ and the client key
    client_key: asyncssh.SSHKey = field(default_factory=new_key)
    misters: list[SimMister] = field(default_factory=list)

    @property
    def key_path(self) -> Path:
        return self.root / "client_key"

    async def add(self, fixture: str, address: str, zaparoo_port: int = 0, ssh_port: int = 0) -> SimMister:
        sd = self.root / "sd" / fixture
        device = FakeDevice.from_fixture(fixture, sd)
        zaparoo = FakeZaparoo(device, address, zaparoo_port)
        ssh = FakeMister(device, new_key(), self.client_key.export_public_key("openssh").decode(), address, ssh_port)
        await zaparoo.start()
        await ssh.start()
        m = SimMister(fixture, address, device, zaparoo, ssh)
        self.misters.append(m)
        return m

    async def stop(self) -> None:
        for m in self.misters:
            await m.zaparoo.stop()
            await m.ssh.stop()

    def config(self, *, ssh: bool = True, backup_dir: Path | None = None) -> dict[str, Any]:
        hosts: list[dict[str, Any]] = []
        for m in self.misters:
            entry: dict[str, Any] = {
                "host": m.address,
                "name": m.device.name,
                "model": m.device.fixture.get("model", "MiSTer FPGA"),
                "zaparoo_port": m.zaparoo.port,
            }
            if ssh:
                entry["ssh"] = {"port": m.ssh.port, "key": str(self.key_path), "host_key": m.ssh.public_host_key}
            hosts.append(entry)
        data: dict[str, Any] = {"hosts": hosts}
        if backup_dir is not None:
            data["backup_dir"] = str(backup_dir)
        return data

    def write_config(self, config_dir: Path, **kwargs: Any) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_key.write_private_key(str(self.key_path))
        self.key_path.chmod(0o600)
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / "config.json"
        path.write_text(json.dumps(self.config(**kwargs), indent=2) + "\n")
        return path
