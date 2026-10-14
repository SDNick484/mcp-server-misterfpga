"""This server's rate limits: how fast each kind of action may repeat, per MiSTer.

The mechanism (TokenBucket, refusal) is shared with the siblings in
ratelimit.py; these numbers are about a MiSTer. Each bucket allows a burst,
then a sustained rate (capacity, tokens per second):

- launches and returns to the menu: 4, then one per 15 s. Each one reloads the
  FPGA; a model flipping between games in a loop is the case to stop.
- screenshots: 6, then one per 10 s. Each writes a PNG to the SD card.
- MiSTer.ini writes: 10, then one per 6 s. Each keeps a backup file.
- NFC writes and mappings: 10, then one per 3 s.
- updates: one per hour. Update All replaces cores and may reboot.
"""

from __future__ import annotations

from .ratelimit import TokenBucket

LAUNCH = (4.0, 1 / 15)
SCREENSHOT = (6.0, 1 / 10)
INI_WRITE = (10.0, 1 / 6)
NFC = (10.0, 1 / 3)
UPDATE = (1.0, 1 / 3600)

SEARCH_MAX = 50
HISTORY_MAX = 50
INI_BACKUPS_KEPT = 10  # this server's MiSTer.ini.<timestamp>.bak files on the SD card


def bucket(spec: tuple[float, float]) -> TokenBucket:
    capacity, rate = spec
    return TokenBucket(capacity, rate)
