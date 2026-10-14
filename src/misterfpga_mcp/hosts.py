"""Several MiSTers behind one server: deciding which one a tool call is about.

The rule is Harmony's, unchanged: **never guess.** Two MiSTers usually mean two
TVs, and launching a game, or ending one, on the wrong TV is the failure this
module exists to prevent. So:

- A game ref names its MiSTer: "Family Room/4812". The ref decides; a ``host``
  argument that contradicts it is an error, not a tiebreak.
- An action without a ref or ``host`` acts only when exactly one MiSTer fits:
  the only one configured, or the only one running something (for
  return_to_menu and screenshot). Otherwise the error names the candidates so
  the model can ask, or retry with ``host``.
- Read tools without ``host`` cover every MiSTer (the Onkyo/webOS convention).

Names are compared ignoring case, spaces and punctuation ("family room" is
"Family Room"), and a MiSTer also answers to its address.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .config import HostSettings, Settings
from .errors import MisterError
from .host import MisterHost

log = logging.getLogger(__name__)

STOP_TIMEOUT = 5.0  # seconds per MiSTer to close its connections at shutdown

HostFactory = Callable[[HostSettings, Settings], MisterHost]


def _list(items: list[str]) -> str:
    return ", ".join(items) if items else "(none)"


class HostRegistry:
    # A client may call a tool the moment it launches us, while the MiSTers are still connecting. The first
    # call waits up to this long for each one's first attempt to finish, once; later calls never wait, so a
    # MiSTer that drops is reported at once.
    startup_grace = 5.0

    def __init__(self, settings: Settings, host_factory: HostFactory = MisterHost) -> None:
        self.settings = settings
        self.hosts = [host_factory(h, settings) for h in settings.hosts]
        self._ready_checked = False

    async def start(self) -> None:
        for problem in self.settings.problems:
            log.warning("Config: %s", problem)
        if self.settings.dry_run:
            log.warning("DRY RUN: MiSTers are read, but nothing that changes anything is done.")
        for h in self.hosts:
            await h.start()

    async def ready(self) -> None:
        if self._ready_checked:
            return
        self._ready_checked = True
        pending = [h.zaparoo.tried.wait() for h in self.hosts if not h.zaparoo.tried.is_set()]
        if pending:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.startup_grace):
                    await asyncio.gather(*pending)

    async def stop(self) -> None:
        async def stop_one(h: MisterHost) -> None:
            try:
                async with asyncio.timeout(STOP_TIMEOUT):
                    await h.stop()
            except TimeoutError:
                log.warning("%s: didn't close within %.0fs; abandoning it", h.name, STOP_TIMEOUT)

        with contextlib.suppress(Exception):
            await asyncio.gather(*(stop_one(h) for h in self.hosts))

    def names(self) -> str:
        return _list([h.name for h in self.hosts])

    def candidates(self, host: str | None) -> list[MisterHost]:
        """The MiSTers a read may cover: the named one, or all of them."""
        if not self.hosts:
            raise MisterError(
                "No MiSTer is configured. Run `mcp-server-misterfpga check --host <address>` for each one (the "
                "README's 'Set up your MiSTer'), or set MISTERFPGA_HOSTS."
            )
        if host is None:
            return list(self.hosts)
        match = [h for h in self.hosts if h.answers_to(host)]
        if not match:
            raise MisterError(f"No MiSTer named {host!r}. MiSTers: {self.names()}")
        return match[:1]

    def one(self, host: str | None, what: str) -> MisterHost:
        """Exactly one MiSTer for an action that has no ref to go by."""
        pool = self.candidates(host)
        if len(pool) == 1:
            return pool[0]
        raise MisterError(f"Which MiSTer should {what}? Pass host: {self.names()}.")

    def game(self, game_ref: str, host: str | None) -> tuple[MisterHost, int]:
        """Split a ref from search_games ("Family Room/4812") into its MiSTer and media id."""
        # Host names can't contain "/" (config.py), so the first "/" is the split, and the rest must be a number.
        name, sep, raw_id = game_ref.strip().partition("/")
        if not sep or not raw_id.isdigit():
            raise MisterError(
                f"{game_ref!r} isn't a game ref. Refs look like 'Family Room/4812' and come from search_games or "
                "play_history."
            )
        match = [h for h in self.candidates(None) if h.answers_to(name)]
        if not match:
            raise MisterError(f"{game_ref!r} names no configured MiSTer. MiSTers: {self.names()}")
        target = match[0]
        if host is not None and not target.answers_to(host):
            raise MisterError(
                f"{game_ref!r} is a game on {target.name}, but host={host!r} was given. Refs belong to one MiSTer; "
                "search that MiSTer for the game instead."
            )
        return target, int(raw_id)

    async def busy(self, host: str | None, what: str) -> list[MisterHost]:
        """MiSTers an end-the-game action should act on: the named one, or the one that's running something."""
        if host is not None:
            return self.candidates(host)
        pool = self.candidates(None)
        if len(pool) == 1:
            return pool

        async def running(h: MisterHost) -> bool:
            try:
                return await h.busy() is not None
            except MisterError:
                return False

        flags = await asyncio.gather(*(running(h) for h in pool))
        on = [h for h, f in zip(pool, flags, strict=True) if f]
        if len(on) > 1:
            raise MisterError(
                f"Games are running on {_list([h.name for h in on])}; pass host to choose where to {what}."
            )
        return on
