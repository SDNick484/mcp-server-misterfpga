"""Settings: which MiSTers, what they are, how to reach them, and where backups go.

``~/.config/mcp-server-misterfpga/config.json`` (or ``$MISTERFPGA_CONFIG_DIR``)::

    {
      "hosts": [
        {"host": "192.168.1.70", "name": "Office", "model": "MiSTer Pi",
         "ssh": {"key": "~/.ssh/mister_ed25519", "host_key": "ssh-ed25519 AAAA..."}},
        {"host": "192.168.1.71", "name": "Family Room", "model": "SuperStation One",
         "manufacturer": "Retro Remake"}
      ],
      "backup_dir": "/mnt/backups/mister-saves"
    }

Per host:
- ``name``: what tools and you call it. Optional: `check` fills it in from the
  MiSTer's hostname, and never overwrites one you wrote. Naming by room
  ("Office") reads best, because ``model`` already says what it is.
- ``model`` / ``manufacturer``: what the device *is* (a MiSTer Pi, a
  SuperStation One). Nothing readable without Zaparoo admin access says which
  board a MiSTer runs on, so it's configured. Shown in get_status and in the
  play tracker's Home Assistant device.
- ``zaparoo_port``: 7497 unless you changed Zaparoo's api_port (or for the
  simulator).
- ``ssh``: optional. Without it, INI editing, save backups, attract-mode
  detection and the running core's name are unavailable for that host.
  ``user`` (root), ``port`` (22), ``key`` (a private key file; no passwords are
  ever stored), and ``host_key``, the MiSTer's public host key, pinned by
  ``check --ssh``. A host whose key isn't pinned is never connected to.

``backup_dir`` (or ``$MISTERFPGA_BACKUP_DIR``) has no default: backup_saves is
unavailable until you say where backups go. Any writable directory works.

``MISTERFPGA_HOSTS`` (comma-separated addresses) replaces the file's list,
keeping names and settings from the file for hosts it also lists.

Loading never fails: anything wrong becomes a sentence in
``Settings.problems`` (logged at startup, shown by `doctor`) and the bad part is
skipped, so one typo doesn't take every host down.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_ZAPAROO_PORT = 7497  # ASSUMPTION Z-ENDPOINT
DEFAULT_MODEL = "MiSTer FPGA"
DEFAULT_BACKUP_KEEP = 30


def config_dir() -> Path:
    override = os.environ.get("MISTERFPGA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-server-misterfpga"


@dataclass(frozen=True)
class SshSettings:
    user: str = "root"
    port: int = 22  # ASSUMPTION M-SSH
    key: str | None = None  # private key file
    host_key: str | None = None  # the MiSTer's public host key, "ssh-ed25519 AAAA..."

    @property
    def ready(self) -> bool:
        """Both a key to log in with and a pinned host key to check the MiSTer against."""
        return bool(self.key and self.host_key)


@dataclass(frozen=True)
class HostSettings:
    host: str
    name: str | None = None
    model: str = DEFAULT_MODEL
    manufacturer: str | None = None
    zaparoo_port: int = DEFAULT_ZAPAROO_PORT
    ssh: SshSettings | None = None

    @property
    def label(self) -> str:
        return self.name or self.host


@dataclass(frozen=True)
class Settings:
    hosts: tuple[HostSettings, ...] = ()
    backup_dir: Path | None = None
    backup_keep: int = DEFAULT_BACKUP_KEEP
    # Read the MiSTers, but change nothing; every write reports what it would do
    # (MISTERFPGA_DRY_RUN=1, serve --dry-run).
    dry_run: bool = False
    problems: tuple[str, ...] = field(default=())


def flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?"
)
# A name is used in refs ("Office/4812") and MQTT topics, so it can't contain "/" or control characters.
_NAME = re.compile(r"[^/\x00-\x1f]{1,40}")


def host_problem(host: str) -> str | None:
    """Why `host` can't be a MiSTer's address, or None if it can."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if ":" in host:
        return f"host {host!r} includes a port; put the port in zaparoo_port (or ssh.port) instead"
    if not _HOSTNAME.fullmatch(host):
        return f"host {host!r} is not an IP address or hostname"
    return None


def _port(value: Any, default: int, what: str, problems: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536:
        return value
    problems.append(f"ignoring {what} {value!r}: expected a number from 1 to 65535")
    return default


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _ssh(raw: Any, label: str, problems: list[str]) -> SshSettings | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(f'{label}: ssh should be an object like {{"key": "~/.ssh/mister_ed25519"}}; ignoring it')
        return None
    if "password" in raw:
        problems.append(f"{label}: ssh passwords aren't supported; use a key (ssh-copy-id). Ignoring the password")
    key = _text(raw.get("key"))
    return SshSettings(
        user=_text(raw.get("user")) or "root",
        port=_port(raw.get("port"), 22, f"{label} ssh.port", problems),
        key=str(Path(key).expanduser()) if key else None,
        host_key=_text(raw.get("host_key")),
    )


def _hosts(data: dict[str, Any], problems: list[str]) -> list[HostSettings]:
    hosts: list[HostSettings] = []
    for e in data.get("hosts") or []:
        if isinstance(e, str) and e.strip():
            e = {"host": e}
        if not (isinstance(e, dict) and _text(e.get("host"))):
            problems.append(f"ignoring host entry {e!r} in config.json: expected {{host, name, model, ...}}")
            continue
        address = str(e["host"]).strip()
        if (why := host_problem(address)) is not None:
            problems.append(f"ignoring a host: {why}")
            continue
        if any(h.host == address for h in hosts):
            problems.append(f"host {address} is listed twice in config.json; using the first entry")
            continue
        name = _text(e.get("name"))
        if name is not None and not _NAME.fullmatch(name):
            problems.append(f"{address}: name {name!r} can't contain '/' and must be 1-40 characters; ignoring it")
            name = None
        label = name or address
        hosts.append(
            HostSettings(
                host=address,
                name=name,
                model=_text(e.get("model")) or DEFAULT_MODEL,
                manufacturer=_text(e.get("manufacturer")),
                zaparoo_port=_port(e.get("zaparoo_port"), DEFAULT_ZAPAROO_PORT, f"{label} zaparoo_port", problems),
                ssh=_ssh(e.get("ssh"), label, problems),
            )
        )
    return hosts


def read_config(path: Path, problems: list[str] | None = None) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        if problems is not None:
            problems.append(f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}); ignoring it")
        return {}
    if not isinstance(data, dict):
        if problems is not None:
            problems.append(f"{path} should hold a JSON object, not {type(data).__name__}; ignoring it")
        return {}
    return data


def name_key(name: str) -> str:
    """How names are compared: case, spaces and punctuation don't matter ("family room" is "Family Room")."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def load_settings() -> Settings:
    problems: list[str] = []
    data = read_config(config_dir() / "config.json", problems)
    hosts = _hosts(data, problems)
    env = os.environ.get("MISTERFPGA_HOSTS")
    if env:
        known = {h.host: h for h in hosts}
        hosts = []
        for address in dict.fromkeys(a.strip() for a in env.split(",") if a.strip()):
            if (why := host_problem(address)) is not None:
                problems.append(f"ignoring a host from MISTERFPGA_HOSTS: {why}")
            else:
                hosts.append(known.get(address, HostSettings(address)))
    seen: dict[str, str] = {}
    for h in hosts:
        if h.name:
            key = name_key(h.name)
            if key in seen:
                problems.append(
                    f"two MiSTers are named {h.name!r} ({seen[key]} and {h.host}); give one a different name in "
                    "config.json, or tools can only tell them apart by address"
                )
            seen[key] = h.host
    backup = os.environ.get("MISTERFPGA_BACKUP_DIR") or _text(data.get("backup_dir"))
    keep = data.get("backup_keep", DEFAULT_BACKUP_KEEP)
    if not (isinstance(keep, int) and not isinstance(keep, bool) and 1 <= keep <= 1000):
        problems.append(f"ignoring backup_keep {keep!r}: expected a number from 1 to 1000")
        keep = DEFAULT_BACKUP_KEEP
    return Settings(
        hosts=tuple(hosts),
        backup_dir=Path(backup).expanduser() if backup else None,
        backup_keep=keep,
        dry_run=flag(os.environ.get("MISTERFPGA_DRY_RUN")),
        problems=tuple(problems),
    )


def save_host(new: HostSettings) -> Path:
    """Add a host to config.json, or fill in what it's missing. Never overwrites what you wrote.

    Keeps every other host and key. Fields already set on an existing entry stay
    as they are; only empty ones are filled from ``new``.
    """
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.json"
    data = read_config(path)
    entries: list[Any] = list(data.get("hosts") or [])
    for i, e in enumerate(entries):
        entry = {"host": e} if isinstance(e, str) else e
        if isinstance(entry, dict) and entry.get("host") == new.host:
            entries[i] = _merge(entry, new)
            break
    else:
        entries.append(_merge({"host": new.host}, new))
    data["hosts"] = entries
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path


def _merge(entry: dict[str, Any], new: HostSettings) -> dict[str, Any]:
    out = dict(entry)
    if new.name and not _text(out.get("name")):
        out["name"] = new.name
    if new.model != DEFAULT_MODEL and not _text(out.get("model")):
        out["model"] = new.model
    if new.manufacturer and not _text(out.get("manufacturer")):
        out["manufacturer"] = new.manufacturer
    if new.zaparoo_port != DEFAULT_ZAPAROO_PORT and "zaparoo_port" not in out:
        out["zaparoo_port"] = new.zaparoo_port
    if new.ssh is not None:
        ssh = dict(out.get("ssh") or {}) if isinstance(out.get("ssh"), dict) else {}
        defaults = SshSettings()
        for key, value in (("user", new.ssh.user), ("port", new.ssh.port), ("key", new.ssh.key)):
            if value not in (None, getattr(defaults, key)) and key not in ssh:
                ssh[key] = value
        if new.ssh.host_key and not ssh.get("host_key"):
            ssh["host_key"] = new.ssh.host_key
        out["ssh"] = ssh
    return out


def with_dry_run(settings: Settings, dry_run: bool) -> Settings:
    return replace(settings, dry_run=dry_run)
