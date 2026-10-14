"""Entry point: `mcp-server-misterfpga [serve|check|call|doctor|simulate]`.

serve      speak MCP: stdio by default (the client launches it), or --http for
           a long-lived service (remote.py). This server is meant to stay on
           your LAN (docs/DESIGN.md §11). --dry-run reads the MiSTers but
           changes nothing.
check      first contact with one MiSTer: confirm Zaparoo answers, and with
           --ssh, show its SSH host key fingerprint, pin it once you confirm,
           and test the login. Saves the MiSTer to config.json.
call       call one tool exactly as the model would (through an in-process MCP
           client, so arguments are validated the same way) and print the
           result: `call launch game="Family Room/4812"`; `call tools`.
doctor     check each MiSTer layer by layer and say what failed (doctor.py);
           --dump writes redacted recordings of its read-only answers.
simulate   run two simulated MiSTers (fake Zaparoo + fake SSH) on 127.0.0.1
           and 127.0.0.2, and write a config pointing at them, so everything
           above works without hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__, doctor, remote
from .config import HostSettings, SshSettings, config_dir, flag, host_problem, load_settings, save_host
from .errors import MisterError
from .logsafe import setup_logging

HTTP_PORT = 8716  # Onkyo 8711, Shield 8712, Harmony 8713, Sofabaton 8714, webOS 8715
HTTP_PATH = "/misterfpga/mcp"


def _setup_logging(args: argparse.Namespace) -> None:
    verbose = getattr(args, "verbose", False)
    unredacted = getattr(args, "no_redact", False) or flag(os.environ.get("MISTERFPGA_LOG_UNREDACTED"))
    # A server's log is its record (INFO); a one-off command's output is the point, so only warnings join it.
    quiet = args.cmd in ("call", "check", "doctor")
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    setup_logging(level, redacted=not unredacted)
    # asyncssh logs every channel at INFO; that's noise unless you're debugging SSH itself.
    logging.getLogger("asyncssh").setLevel(logging.DEBUG if verbose else logging.WARNING)


# --- check ------------------------------------------------------------------------------------
def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("  (not a terminal: pass --yes to accept)", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


async def _cmd_check(args: argparse.Namespace) -> int:
    from .host import MisterHost
    from .ssh import Command, MisterShell, fingerprint, read_host_key

    if (why := host_problem(args.host)) is not None:
        print(why, file=sys.stderr)
        return 2
    settings = load_settings()
    existing = next((h for h in settings.hosts if h.host == args.host), None)
    name = args.name or (existing.name if existing else None)
    print(f"Checking {args.host}")

    host = MisterHost(HostSettings(args.host, name, zaparoo_port=args.zaparoo_port), settings)
    await host.zaparoo.start()
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(8):
            await host.zaparoo.tried.wait()
    if host.zaparoo.connected:
        print(f"  Zaparoo Core {host.zaparoo.version}: ok")
    else:
        print(f"  Zaparoo: {host.zaparoo.error}")
    await host.zaparoo.stop()

    ssh: SshSettings | None = existing.ssh if existing else None
    if args.ssh:
        key = str(Path(args.ssh_key).expanduser()) if args.ssh_key else (ssh.key if ssh else None)
        if not key:
            print("  --ssh needs --ssh-key PATH (the private key you added with ssh-copy-id)", file=sys.stderr)
            return 2
        pinned = ssh.host_key if ssh else None
        try:
            offered = await read_host_key(args.host, args.ssh_port)
        except (OSError, MisterError, TimeoutError) as exc:
            print(f"  SSH: can't reach port {args.ssh_port}: {exc}", file=sys.stderr)
            return 1
        print(f"  SSH host key: {fingerprint(offered)}")
        if pinned and pinned.split()[:2] != offered.split()[:2]:
            print(
                "  This is NOT the host key pinned in config.json. If you reflashed this MiSTer, remove ssh.host_key "
                "and run check --ssh again.",
                file=sys.stderr,
            )
            return 1
        if not pinned and not _confirm("  Is this the MiSTer you expect? Pin this host key?", args.yes):
            print("  Not pinned; nothing saved.")
            return 1
        ssh = SshSettings(user=args.ssh_user, port=args.ssh_port, key=key, host_key=offered)
        shell = MisterShell(name or args.host, args.host, ssh)
        try:
            core = (await shell.run(Command.CORENAME)).strip()
            hostname = (await shell.run(Command.HOSTNAME)).strip()
            print(f"  SSH login as {ssh.user}: ok (running core: {core}, hostname: {hostname})")
            name = name or hostname or None
        except MisterError as exc:
            print(f"  SSH login: {exc}", file=sys.stderr)
            return 1
        finally:
            await shell.close()

    model = args.model or (existing.model if existing else None) or "MiSTer FPGA"
    path = save_host(
        HostSettings(
            args.host, name, model=model, manufacturer=args.manufacturer, zaparoo_port=args.zaparoo_port, ssh=ssh
        )
    )
    print(f"Saved {name or args.host} ({model}) to {path}")
    if model == "MiSTer FPGA":
        print("  Tip: --model says what this is (e.g. 'MiSTer Pi', 'SuperStation One'); it names it in Home Assistant.")
    return 0


# --- call -------------------------------------------------------------------------------------
async def _cmd_call(tool: str, raw_args: list[str]) -> int:
    """One tool call through the real MCP layer, as the model would make it."""
    from mcp import Client

    from .server import mcp

    tool_args: dict[str, object] = {}
    for pair in raw_args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return 2
        try:
            tool_args[key] = json.loads(raw)  # limit=5 -> 5, value=null -> None
        except json.JSONDecodeError:
            tool_args[key] = raw  # game=Office/101 -> a string
    async with Client(mcp) as c:
        if tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<16} {(t.description or '').splitlines()[0]}")
            return 0
        result = await c.call_tool(tool, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    body: Any = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


# --- doctor -----------------------------------------------------------------------------------
async def _cmd_doctor(args: argparse.Namespace) -> int:
    reports = await doctor.run(load_settings(), args.host, args.timeout, Path(args.dump) if args.dump else None)
    print(doctor.render(reports, args.json))
    return doctor.exit_code(reports)


# --- simulate ---------------------------------------------------------------------------------
async def _cmd_simulate(args: argparse.Namespace) -> int:
    from .sim.harness import Simulation

    root = Path(args.dir).expanduser().resolve()
    sim = Simulation(root / "sim")
    await sim.add("office", "127.0.0.1", args.zaparoo_port, args.ssh_port)
    await sim.add("family_room", "127.0.0.2", args.zaparoo_port, args.ssh_port)
    backups = root / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    sim.write_config(root / "config", backup_dir=backups)
    print("Two simulated MiSTers:")
    for m in sim.misters:
        print(
            f"  {m.device.name} ({m.device.fixture['model']}) on {m.address}: Zaparoo :{m.zaparoo.port}, "
            f"SSH :{m.ssh.port}"
        )
    print("\nIn another terminal:")
    print(f"  export MISTERFPGA_CONFIG_DIR={root / 'config'}")
    print("  mcp-server-misterfpga doctor")
    print("  mcp-server-misterfpga call search_games query=metroid")
    print('  mcp-server-misterfpga call launch game="Office/101"')
    print("Ctrl+C to stop.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await sim.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-misterfpga", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging, including protocol traffic")
    common.add_argument(
        "--no-redact",
        action="store_true",
        help="show addresses in logs (MISTERFPGA_LOG_UNREDACTED=1); secrets stay hidden",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio by default, or --http)")
    p_serve.add_argument("--dry-run", action="store_true", help="change nothing (MISTERFPGA_DRY_RUN=1)")
    remote.add_http_arguments(p_serve, default_port=HTTP_PORT, default_path=HTTP_PATH)

    p_check = sub.add_parser("check", parents=[common], help="first contact with a MiSTer; save it to config.json")
    p_check.add_argument("--host", required=True, help="the MiSTer's IP address or hostname")
    p_check.add_argument("--name", help="what to call it, e.g. a room: 'Family Room' (default: its hostname)")
    p_check.add_argument("--model", help="what it is, e.g. 'MiSTer Pi' or 'SuperStation One'")
    p_check.add_argument("--manufacturer", help="e.g. 'Retro Remake'")
    p_check.add_argument("--zaparoo-port", type=int, default=7497)
    p_check.add_argument("--ssh", action="store_true", help="also set up SSH: pin the host key and test the login")
    p_check.add_argument("--ssh-key", help="private key file you added with ssh-copy-id")
    p_check.add_argument("--ssh-user", default="root")
    p_check.add_argument("--ssh-port", type=int, default=22)
    p_check.add_argument("--yes", action="store_true", help="pin the SSH host key without asking")

    p_call = sub.add_parser("call", parents=[common], help="call one tool as the model would and print the result")
    p_call.add_argument("tool", help="tool name, or 'tools' to list them")
    p_call.add_argument("args", nargs="*", metavar="key=value", help="tool arguments (values are JSON if they parse)")
    p_call.add_argument("--dry-run", action="store_true", help="change nothing")

    p_doc = sub.add_parser("doctor", parents=[common], help="check each MiSTer layer by layer (read-only)")
    p_doc.add_argument("--host", help="only this MiSTer")
    p_doc.add_argument("--json", action="store_true", help="machine-readable output")
    p_doc.add_argument("--timeout", type=float, default=4.0, help="seconds per network step (default 4)")
    p_doc.add_argument("--dump", metavar="DIR", help="also write redacted recordings of the answers to DIR")

    p_sim = sub.add_parser("simulate", parents=[common], help="run two simulated MiSTers, for testing without one")
    p_sim.add_argument(
        "--dir",
        default=str(config_dir().parent / "mcp-server-misterfpga-sim"),
        help="where the simulated SD cards, config and backups go",
    )
    p_sim.add_argument("--zaparoo-port", type=int, default=7497)
    p_sim.add_argument("--ssh-port", type=int, default=2222)
    return parser


def normalize(argv: list[str]) -> list[str]:
    """No subcommand means serve: `mcp-server-misterfpga --http` is `serve --http`."""
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        return ["serve", *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(normalize(sys.argv[1:] if argv is None else argv))
    if getattr(args, "dry_run", False):
        os.environ["MISTERFPGA_DRY_RUN"] = "1"  # the server's lifespan reads settings from the environment
    _setup_logging(args)

    commands = {
        "check": lambda: _cmd_check(args),
        "call": lambda: _cmd_call(args.tool, args.args),
        "doctor": lambda: _cmd_doctor(args),
    }
    if args.cmd in commands:
        try:
            code = asyncio.run(commands[args.cmd]())
            sys.stdout.flush()
        except BrokenPipeError:  # `call ... | head`: the reader left; that's not an error
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            code = 0
        sys.exit(code)
    if args.cmd == "simulate":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(asyncio.run(_cmd_simulate(args)))
        return

    from .server import mcp  # imported late: check and doctor don't need the server

    if args.http:
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-misterfpga: {exc}\n")
    else:
        mcp.run()
