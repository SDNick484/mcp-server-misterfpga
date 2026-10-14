"""End to end: launch the installed entry point and talk MCP over stdio.

Covers what the in-process tests skip: the console script, `serve` as the
default, real settings loading, and the stdio transport across a process
boundary. The server runs with no MiSTer configured, so no network is needed.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

from .test_tools import TOOL_NAMES

pytestmark = pytest.mark.anyio


async def test_serve_over_stdio(tmp_path):
    exe = shutil.which("mcp-server-misterfpga", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "MISTERFPGA_CONFIG_DIR": str(tmp_path)}
    env.pop("MISTERFPGA_HOSTS", None)
    async with Client(StdioServerParameters(command=exe, args=[], env=env)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOL_NAMES
        assert (await c.call_tool("get_status", {})).is_error  # no MiSTer configured: says how to add one
        resources = {str(r.uri) for r in (await c.list_resources()).resources}
        assert "misterfpga://ini-keys" in resources
