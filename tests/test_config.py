"""Settings: config.json, MISTERFPGA_* overrides, problems as sentences, and save_host never overwriting you."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from misterfpga_mcp.config import DEFAULT_MODEL, HostSettings, SshSettings, config_dir, load_settings, save_host


def write(data: object) -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(data))
    return d / "config.json"


def test_no_config_is_no_hosts_and_no_problems():
    s = load_settings()
    assert s.hosts == () and s.problems == () and s.backup_dir is None and s.dry_run is False


def test_full_entry():
    write(
        {
            "hosts": [
                {
                    "host": "192.168.1.71",
                    "name": "Family Room",
                    "model": "SuperStation One",
                    "manufacturer": "Retro Remake",
                    "zaparoo_port": 7500,
                    "ssh": {"key": "~/.ssh/mister", "host_key": "ssh-ed25519 AAAA", "port": 2222},
                },
            ],
            "backup_dir": "~/mister-saves",
            "backup_keep": 7,
        }
    )
    s = load_settings()
    (h,) = s.hosts
    assert (h.name, h.model, h.manufacturer, h.zaparoo_port) == (
        "Family Room",
        "SuperStation One",
        "Retro Remake",
        7500,
    )
    assert h.ssh is not None and h.ssh.port == 2222 and h.ssh.ready and h.ssh.key.endswith("/.ssh/mister")
    assert s.backup_dir == Path("~/mister-saves").expanduser() and s.backup_keep == 7
    assert s.problems == ()


def test_problems_are_sentences_and_the_rest_still_loads():
    write(
        {
            "hosts": [
                {"host": "192.168.1.70:7497"},
                {"host": "192.168.1.71", "name": "a/b"},
                {"host": "192.168.1.72", "zaparoo_port": 99999, "ssh": {"password": "1"}},
                {"host": "192.168.1.72"},
                42,
            ],
            "backup_keep": 0,
        }
    )
    s = load_settings()
    assert [h.host for h in s.hosts] == ["192.168.1.71", "192.168.1.72"]
    assert s.hosts[0].name is None and s.hosts[1].zaparoo_port == 7497
    text = " | ".join(s.problems)
    for expected in (
        "includes a port",
        "can't contain '/'",
        "expected a number from 1 to 65535",
        "passwords aren't supported",
        "listed twice",
        "ignoring host entry 42",
        "backup_keep",
    ):
        assert expected in text, expected


def test_two_hosts_with_one_name_is_a_problem():
    write({"hosts": [{"host": "10.0.0.1", "name": "Den"}, {"host": "10.0.0.2", "name": "den"}]})
    assert "two MiSTers are named 'den'" in load_settings().problems[0]


def test_env_overrides(monkeypatch: pytest.MonkeyPatch):
    write({"hosts": [{"host": "10.0.0.1", "name": "Den", "model": "MiSTer Pi"}], "backup_dir": "/a"})
    monkeypatch.setenv("MISTERFPGA_HOSTS", "10.0.0.1, 10.0.0.9")
    monkeypatch.setenv("MISTERFPGA_BACKUP_DIR", "/b")
    monkeypatch.setenv("MISTERFPGA_DRY_RUN", "yes")
    s = load_settings()
    assert [(h.host, h.name, h.model) for h in s.hosts] == [
        ("10.0.0.1", "Den", "MiSTer Pi"),
        ("10.0.0.9", None, DEFAULT_MODEL),
    ]
    assert s.backup_dir == Path("/b") and s.dry_run is True


def test_save_host_fills_in_but_never_overwrites():
    write({"hosts": [{"host": "10.0.0.1", "name": "Mine", "note": "kept"}], "other": True})
    save_host(
        HostSettings("10.0.0.1", "MiSTer", model="MiSTer Pi", ssh=SshSettings(key="/k", host_key="ssh-ed25519 AAAA"))
    )
    save_host(HostSettings("10.0.0.2", "Second"))
    data = json.loads((config_dir() / "config.json").read_text())
    assert data["other"] is True
    first, second = data["hosts"]
    assert first == {
        "host": "10.0.0.1",
        "name": "Mine",
        "note": "kept",
        "model": "MiSTer Pi",
        "ssh": {"key": "/k", "host_key": "ssh-ed25519 AAAA"},
    }
    assert second == {"host": "10.0.0.2", "name": "Second"}
    assert (config_dir() / "config.json").stat().st_mode & 0o777 == 0o600  # it can hold a key path
