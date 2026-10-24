"""This server's real tools over Streamable HTTP, behind the Cloudflare Access check.

test_remote.py (shared with the sibling servers) tests the transport with a one-tool
server. This runs *these* tools end to end, the way the OpenRC service serves them:
uvicorn on a free port, MCP over HTTP, Access assertions signed with a throwaway RSA
key (so nothing here needs Cloudflare), and the two simulated MiSTers behind it all.
Same shape as mcp-server-batocera's test_http_tools.py.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from misterfpga_mcp import cli, remote, server

from .conftest import family, ok, wait_until

TEAM = "example.cloudflareaccess.com"
AUD = "misterfpga-aud"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POLICY = remote.AccessPolicy(TEAM, (AUD,))


def token(**overrides: object) -> str:
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "player@example.com"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


async def public_key(_token: str) -> object:
    return KEY.public_key()


@asynccontextmanager
async def serving(policy: remote.AccessPolicy = POLICY) -> AsyncIterator[str]:
    # The server's lifespan reads config.json from MISTERFPGA_CONFIG_DIR, which `configured` wrote.
    cfg = remote.HttpConfig(path="/misterfpga/mcp", access=policy)
    app = remote.build_app(server.mcp, cfg, remote.AccessVerifier(policy, public_key))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(srv.serve, [sock])
        while not srv.started:
            await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/misterfpga/mcp"
        srv.should_exit = True


def client(url: str, assertion: str | None) -> Client:
    headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


@pytest.mark.anyio
async def test_tools_work_over_http_with_a_valid_assertion(configured):
    async with serving() as url, client(url, token()) as c:
        await wait_until(lambda: all(h.zaparoo.tried.is_set() for h in server.registry().hosts))
        hosts = ok(await c.call_tool("get_status", {}))["hosts"]
        assert {h["name"]: h["reachable"] for h in hosts} == {"Office": True, "Family Room": True}
        body = ok(await c.call_tool("launch", {"game": "Family Room/4812"}))
        assert body["outcome"] == "done"
    assert family(configured).device.core == "SNES"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "assertion",
    [
        None,  # no header: didn't come through Access
        "garbage",
        token(exp=int(time.time()) - 3600),  # expired
        token(aud=["someone-elses-app"]),  # wrong audience
        token(iss="https://evil.cloudflareaccess.com"),  # wrong issuer
    ],
    ids=["missing", "garbage", "expired", "wrong-audience", "wrong-issuer"],
)
async def test_bad_assertions_never_reach_a_mister(configured, assertion):
    mister = family(configured)
    super_metroid = mister.device.by_id(4812)
    assert super_metroid is not None
    playing = mister.device.start_game(super_metroid)
    async with serving() as url, httpx2.AsyncClient() as http:
        headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "return_to_menu"}}
        resp = await http.post(url, json=body, headers=headers)
        assert (resp.status_code, resp.json()) == (403, {"error": "forbidden"})
    # The game is still running, and no MiSTer was asked to stop it.
    assert mister.device.active == playing
    assert all("stop" not in m.zaparoo.methods for m in configured.misters)


@pytest.mark.anyio
async def test_allowed_emails_narrow_what_access_admits(configured):
    narrow = remote.AccessPolicy(TEAM, (AUD,), frozenset({"owner@example.com"}))
    async with serving(narrow) as url, httpx2.AsyncClient() as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        resp = await http.post(url, json=body, headers={remote.ACCESS_HEADER: token(email="guest@example.com")})
        assert resp.status_code == 403


def test_http_flags_and_defaults():
    args = cli.build_parser().parse_args(["serve", "--http"])
    cfg = remote.http_config(args, env={})
    assert (args.http, cfg.bind, cfg.port, cfg.path, cfg.access) == (True, "127.0.0.1", 8716, "/misterfpga/mcp", None)
    # The service's env file sets these; flags would win over them.
    env = {"MCP_HTTP_PORT": "9001", "CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD}
    cfg = remote.http_config(args, env=env)
    assert (cfg.port, cfg.access) == (9001, POLICY)


def test_unsafe_bind_is_refused_before_serving(tmp_path):
    exe = shutil.which("mcp-server-misterfpga", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CF_ACCESS_", "MCP_"))}
    env["MISTERFPGA_CONFIG_DIR"] = str(tmp_path)
    out = subprocess.run(
        [exe, "serve", "--http", "--bind", "0.0.0.0"], env=env, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 2
    assert "Refusing to serve on 0.0.0.0 without Cloudflare Access" in out.stderr
