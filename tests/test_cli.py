"""The CLI, run as a separate process against simulated MiSTers: call, doctor and check."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from misterfpga_mcp.cli import normalize

from .conftest import family
from .test_tools import TOOL_NAMES

pytestmark = pytest.mark.anyio

EXE = shutil.which("mcp-server-misterfpga", path=os.path.dirname(sys.executable))
needs_exe = pytest.mark.skipif(EXE is None, reason="entry point not installed (pip install -e .)")


async def run(*args: str, env_dir: Path) -> tuple[int, str, str]:
    assert EXE is not None
    env = {**os.environ, "MISTERFPGA_CONFIG_DIR": str(env_dir)}
    proc = await asyncio.create_subprocess_exec(
        EXE, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


def test_no_subcommand_means_serve():
    assert normalize([]) == ["serve"]
    assert normalize(["--http"]) == ["serve", "--http"]
    assert normalize(["doctor"]) == ["doctor"]


@needs_exe
async def test_call_lists_and_calls_tools(configured, tmp_path):
    code, out, _ = await run("call", "tools", env_dir=tmp_path / "config")
    assert code == 0 and {line.split()[0] for line in out.splitlines()} == TOOL_NAMES
    code, out, err = await run("call", "search_games", "query=gunstar", env_dir=tmp_path / "config")
    assert code == 0, err
    assert json.loads(out)["results"][0]["ref"] == "Family Room/4901"
    code, _, err = await run("call", "launch", "game=Nowhere/1", env_dir=tmp_path / "config")
    assert code == 1 and "names no configured MiSTer" in err


@needs_exe
async def test_doctor_reports_each_layer(configured, tmp_path):
    dump = tmp_path / "dump"
    code, out, err = await run("doctor", "--json", "--dump", str(dump), env_dir=tmp_path / "config")
    assert code == 0, out + err
    reports = {r["host"]: {(s["layer"], s["check"]): s["level"] for s in r["steps"]} for r in json.loads(out)}
    office_steps = reports["Office"]
    assert office_steps[("zaparoo", "WebSocket API")] == "ok" and office_steps[("ssh", "login")] == "ok"
    assert office_steps[("ssh", "tracking settings")] == "warn"  # log_file_entry=0 in Office's fixture
    assert reports["Family Room"][("zaparoo", "media library")] == "ok"
    assert reports["Office"][("backups", "backup_dir")] == "ok"
    recorded = json.loads((dump / "office.json").read_text())
    assert recorded["version"]["platform"] == "mister" and "media.search" in recorded


@needs_exe
async def test_check_pins_the_ssh_host_key_and_saves_the_model(sim, tmp_path):
    m = family(sim)
    sim.root.mkdir(parents=True, exist_ok=True)
    sim.client_key.write_private_key(str(sim.key_path))
    code, out, err = await run(
        "check", "--host", m.address, "--zaparoo-port", str(m.zaparoo.port), "--ssh", "--ssh-key",
        str(sim.key_path), "--ssh-port", str(m.ssh.port), "--model", "SuperStation One", "--name", "Family Room",
        "--yes", env_dir=tmp_path / "config",
    )  # fmt: skip
    assert code == 0, out + err
    assert "Zaparoo Core 2.20.0: ok" in out and "SSH host key: SHA256:" in out and "SSH login as root: ok" in out
    saved = json.loads((tmp_path / "config" / "config.json").read_text())["hosts"][0]
    assert saved["name"] == "Family Room" and saved["model"] == "SuperStation One"
    assert saved["ssh"]["host_key"] == m.ssh.public_host_key


@needs_exe
async def test_check_refuses_a_different_host_key(sim, tmp_path):
    m = family(sim)
    config = tmp_path / "config"
    sim.write_config(config)
    data = json.loads((config / "config.json").read_text())
    data["hosts"][1]["ssh"]["host_key"] = sim.misters[0].ssh.public_host_key  # Office's key, not Family Room's
    (config / "config.json").write_text(json.dumps(data))
    code, _, err = await run(
        "check", "--host", m.address, "--zaparoo-port", str(m.zaparoo.port), "--ssh", "--ssh-port",
        str(m.ssh.port), env_dir=config,
    )  # fmt: skip
    assert code == 1 and "NOT the host key pinned" in err
