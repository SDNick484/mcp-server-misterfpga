"""Shared fixtures. Tests run against simulated MiSTers on loopback: no MiSTer (or network) needed.

``sim`` starts two: Office (a MiSTer Pi, 127.0.0.1) and Family Room (a
SuperStation One, 127.0.0.2), each a fake Zaparoo Core plus a fake SSH server
over one shared device state (misterfpga_mcp/sim/). *Super Metroid* is in both
libraries on purpose: it's the never-guess case.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from misterfpga_mcp import server
from misterfpga_mcp.sim.harness import SimMister, Simulation

# asyncssh narrates every channel at INFO; failures are easier to read without it.
logging.getLogger("asyncssh").setLevel(logging.WARNING)

ENV = ("MISTERFPGA_HOSTS", "MISTERFPGA_BACKUP_DIR", "MISTERFPGA_DRY_RUN", "MISTERFPGA_LOG_UNREDACTED")


# Async tests use anyio's plugin (pytest.mark.anyio), not pytest-asyncio: the MCP SDK's in-process Client
# needs fixture setup and teardown in one task. The shared tests rely on this fixture too.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MISTERFPGA_CONFIG_DIR", str(tmp_path / "config"))


@pytest.fixture
async def sim(tmp_path: Path) -> AsyncIterator[Simulation]:
    s = Simulation(tmp_path / "sim")
    await s.add("office", "127.0.0.1")
    await s.add("family_room", "127.0.0.2")
    try:
        yield s
    finally:
        await s.stop()


def office(sim: Simulation) -> SimMister:
    return sim.misters[0]


def family(sim: Simulation) -> SimMister:
    return sim.misters[1]


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d


@pytest.fixture
def configured(sim: Simulation, tmp_path: Path, backup_dir: Path) -> Simulation:
    sim.write_config(tmp_path / "config", backup_dir=backup_dir)
    return sim


async def wait_until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for a condition")
        await asyncio.sleep(0.02)


async def connect() -> Client:
    """An in-process MCP client, its server connected to whatever config the test wrote."""
    c = Client(server.mcp)
    await c.__aenter__()
    await wait_until(lambda: all(h.zaparoo.tried.is_set() for h in server.registry().hosts))
    return c


@pytest.fixture
async def mcp_client(configured: Simulation) -> AsyncIterator[Client]:
    c = await connect()
    try:
        yield c
    finally:
        await c.__aexit__(None, None, None)


def text(result: Any) -> str:
    return " ".join(getattr(part, "text", "") for part in result.content)


def ok(result: Any) -> dict[str, Any]:
    assert not result.is_error, text(result)
    body: dict[str, Any] = result.structured_content
    return body


def error(result: Any) -> str:
    assert result.is_error, result.structured_content
    return text(result)
