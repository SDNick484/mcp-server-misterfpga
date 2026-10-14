"""MiSTer.ini: parse it the way MiSTer does, and change one key without disturbing anything else.

How MiSTer reads the file (Main_MiSTer cfg.cpp; ASSUMPTION M-INI-SECTIONS,
M-INI-KEYS):
- ``[name]`` starts a section. ``[MiSTer]`` always applies; ``[arcade]`` and
  ``[arcade_vertical]`` apply to arcade cores; ``[video=<mode>]`` to a video
  mode; anything else is a core name, compared case-insensitively, where a
  trailing ``*`` matches by prefix (``[PSX*]``). A line starting with ``+`` is
  an include. Settings before the first section are ignored.
- ``key=value`` sets a key; keys are case-insensitive. ``;`` starts a comment.
  When a key appears twice, the later one wins.

How we edit it: as text, line by line, so comments, blank lines, order and
your own formatting all survive. Setting a key replaces the value on its last
(effective) line in that section, or adds ``key=value`` at the end of the
section, or adds the section at the end of the file. Removing a key removes
every line of it in that section.

What may be set is MiSTer's own key table (ini_keys.py, generated from
cfg.cpp) minus a deny-list, with each value checked against the key's type and
range. Array keys (DEADZONE, PLAYER_n_CONTROLLER, ...) repeat one key per
entry, so a single key=value edit can't express them; they're read-only here.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .errors import MisterError
from .ini_keys import INI_VARS

# Keys this server never writes, and why. Shown by the misterfpga://ini-keys resource.
DENIED: dict[str, str] = {
    "MAIN": "boots an alternate MiSTer Main binary: a different program runs the whole device",
    "SHARED_FOLDER": "points the shared folder at another path on the device",
    "DEBUG": "debug output, not a setting anyone tunes from a chat",
    "BOOTCORE": "can make every boot skip the menu, which is surprising and awkward to undo",
}

STRING_MAX = 255  # cfg.cpp sizes strings with sizeof(); this stays well inside every one of them
FIXED_SECTIONS = ("MiSTer", "arcade", "arcade_vertical")
_CORE_SECTION = re.compile(r"[A-Za-z0-9_-]{1,40}\*?")
_VIDEO_SECTION = re.compile(r"video=[0-9]{2,5}x[0-9]{2,5}(@[0-9]{1,3}(\.[0-9]{1,3})?)?", re.I)
# cfg.cpp keeps letters, digits, spaces and these characters in a value; anything else is dropped.
_STRING_VALUE = re.compile(r"[A-Za-z0-9 \[\]()\-+/=#$@_,.!*:~]*")


@dataclass(frozen=True)
class KeyInfo:
    name: str  # upper case, as in cfg.cpp
    type: str  # UINT8, INT16, HEX32, FLOAT, STRING, ...ARR
    min: int | float | None
    max: int | float | None

    @property
    def settable(self) -> bool:
        return self.name not in DENIED and not self.type.endswith("ARR")

    def describe(self) -> str:
        if self.type.endswith("ARR"):
            return "array (one line per entry): read-only here"
        if self.type == "STRING":
            return f"text, up to {STRING_MAX} characters"
        if self.type.startswith("HEX"):
            return f"hex 0x{int(self.min or 0):X}-0x{int(self.max or 0):X}"
        return f"{self.type.lower()} {self.min}-{self.max}"


KEYS: dict[str, KeyInfo] = {name: KeyInfo(name, kind, lo, hi) for name, kind, lo, hi in INI_VARS}


def key_info(key: str) -> KeyInfo:
    info = KEYS.get(key.strip().upper())
    if info is None:
        close = difflib.get_close_matches(key.strip().upper(), KEYS, n=3)
        hint = f" Did you mean {', '.join(k.lower() for k in close)}?" if close else ""
        raise MisterError(f"{key!r} isn't a MiSTer.ini setting.{hint} The misterfpga://ini-keys resource lists them.")
    return info


def check_section(section: str) -> str:
    """The section name as it should be written, or an error saying what's allowed."""
    s = section.strip().strip("[]").strip()
    for fixed in FIXED_SECTIONS:
        if s.lower() == fixed.lower():
            return fixed
    if _VIDEO_SECTION.fullmatch(s):
        return s.lower()
    if _CORE_SECTION.fullmatch(s):
        return s
    raise MisterError(
        f"{section!r} isn't a MiSTer.ini section. Use MiSTer, arcade, arcade_vertical, video=<WxH@Hz>, or a core "
        "name such as SNES (optionally ending in * to match by prefix)."
    )


def check_value(info: KeyInfo, value: str | int | float | bool) -> str:
    """The value as it should be written, after checking it against the key's type and range."""
    if not info.settable:
        why = DENIED.get(info.name) or "it's an array key (one line per entry), which a single edit can't express"
        raise MisterError(f"{info.name.lower()} can't be set through this server: {why}.")
    if isinstance(value, bool):
        value = int(value)
    text = str(value).strip()
    kind = info.type
    try:
        if kind == "STRING":
            if len(text) > STRING_MAX or not _STRING_VALUE.fullmatch(text):
                raise ValueError
            return text
        if kind.startswith("HEX"):
            if not text.lower().startswith("0x"):
                raise ValueError
            number: int | float = int(text, 16)
            shown = f"0x{int(number):X}"
        elif kind == "FLOAT":
            number = float(text)
            shown = f"{number:g}"
        else:
            number = int(text, 10)
            shown = str(number)
    except ValueError:
        raise MisterError(f"{text!r} isn't valid for {info.name.lower()} ({info.describe()}).") from None
    if (info.min is not None and number < info.min) or (info.max is not None and number > info.max):
        raise MisterError(f"{text} is out of range for {info.name.lower()} ({info.describe()}).")
    return shown


def _strip_comment(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _section_of(line: str) -> str | None:
    s = _strip_comment(line)
    if s.startswith("[") and "]" in s:
        return s[1 : s.index("]")].strip()
    return None


def _key_value(line: str) -> tuple[str, str] | None:
    s = _strip_comment(line)
    if not s or s.startswith(("[", "+")) or "=" not in s:
        return None
    key, value = s.split("=", 1)
    return key.strip(), value.strip()


@dataclass
class Change:
    section: str
    key: str
    old: str | None
    new: str | None
    text: str  # the whole new file
    diff: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


class IniFile:
    def __init__(self, text: str) -> None:
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.lines = text.splitlines()
        self.trailing_newline = text.endswith(("\n", "\r"))

    def text(self) -> str:
        out = self.newline.join(self.lines)
        return out + self.newline if self.trailing_newline or not out else out

    def _blocks(self) -> list[tuple[str, int, int]]:
        """(section name, first line index after the header, end index) for each section, in order."""
        blocks: list[tuple[str, int, int]] = []
        current: tuple[str, int] | None = None
        for i, line in enumerate(self.lines):
            name = _section_of(line)
            if name is not None:
                if current is not None:
                    blocks.append((current[0], current[1], i))
                current = (name, i + 1)
        if current is not None:
            blocks.append((current[0], current[1], len(self.lines)))
        return blocks

    def sections(self) -> dict[str, dict[str, str]]:
        """{section: {key: effective value}} with keys lower-cased, sections as first written."""
        out: dict[str, dict[str, str]] = {}
        names: dict[str, str] = {}
        for name, start, end in self._blocks():
            shown = names.setdefault(name.lower(), name)
            values = out.setdefault(shown, {})
            for line in self.lines[start:end]:
                kv = _key_value(line)
                if kv:
                    values[kv[0].lower()] = kv[1]
        return out

    def get(self, section: str, key: str) -> str | None:
        for name, values in self.sections().items():
            if name.lower() == section.lower():
                return values.get(key.lower())
        return None

    def set(self, section: str, key: str, value: str | None) -> Change:
        """Set (or, with None, remove) one key in one section. Returns the change, applied in place."""
        before = self.text()
        old = self.get(section, key)
        blocks = [b for b in self._blocks() if b[0].lower() == section.lower()]
        hits = [
            i
            for _, start, end in blocks
            for i in range(start, end)
            if (kv := _key_value(self.lines[i])) and kv[0].lower() == key.lower()
        ]
        if value is None:
            for i in reversed(hits):
                del self.lines[i]
        elif hits:
            i = hits[-1]
            written_key = _key_value(self.lines[i])[0]  # type: ignore[index]
            comment = self.lines[i].split(";", 1)[1] if ";" in self.lines[i] else None
            self.lines[i] = f"{written_key}={value}" + (f" ;{comment}" if comment is not None else "")
        elif blocks:
            _, start, end = blocks[-1]
            at = end
            while at > start and not self.lines[at - 1].strip():
                at -= 1  # before the blank lines that separate it from the next section
            self.lines.insert(at, f"{key.lower()}={value}")
        else:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines += [f"[{section}]", f"{key.lower()}={value}"]
            self.trailing_newline = True
        after = self.text()
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True), "before", "after", n=1
            )
        )
        shown = blocks[0][0] if blocks else section  # an existing section keeps the spelling it has in the file
        return Change(shown, key.lower(), old, value, after, diff)
